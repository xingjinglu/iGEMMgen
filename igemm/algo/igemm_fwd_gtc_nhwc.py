################################################################################
# 
#  MIT License
# 
#  Copyright (c) 2020-2021 Advanced Micro Devices, Inc.
# 
#  Permission is hereby granted, free of charge, to any person obtaining a copy
#  of this software and associated documentation files (the "Software"), to deal
#  in the Software without restriction, including without limitation the rights
#  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#  copies of the Software, and to permit persons to whom the Software is
#  furnished to do so, subject to the following conditions:
# 
#  The above copyright notice and this permission notice shall be included in all
#  copies or substantial portions of the Software.
# 
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#  SOFTWARE.
# 
################################################################################
# pylint: disable=maybe-no-member
from ..codegen import *
from .fma_main_loop import *
from .igemm_base import *
from .global_memory import *
from .shared_memory import *
from .utility import *
from .thread_mapping import *
from .xdlops_mapping import *
from .coalescing_store import *
from .mfma_main_loop import *

def _find_non_1_index_in_list(list_object):
    result_list = list()
    for idx, item in enumerate(list_object):
        assert type(item) is int
        if item != 1:
            result_list.append(idx)
    return result_list

class igemm_fwd_gtc_nhwc_t(mc_base_t):
    '''
                      tensor a (input)                   tensor b (wei)
    thread_lengths  : ta_e, ta_c, ta_nb0, ta_nb1,     tb_e, tb_c, tb_k0, tb_k1
    cluster_lengths : ca_e, ca_c, ca_nb0, ca_nb1,     cb_e, cb_c, cb_k0, cb_k1

    for a/b tensor, always load gemm_k dimension first.

    '''
    def __init__(self, mc, tunable):
        assert type(tunable) is igemm_gtc_tunable_parameter_t
        mc_base_t.__init__(self, mc)
        self.tunable = tunable
        self.global_load_in = self.global_load_in_t(mc, self)
        self.global_load_wei = self.global_load_wei_t(mc, self)
        self.shared_store_in = self.shared_store_in_t(mc, self)
        self.shared_store_wei = self.shared_store_wei_t(mc, self)

        in_thread_copy_index, wei_thread_copy_index = self.get_thread_copy_index()
        self.in_thread_copy_ndim = len(in_thread_copy_index)
        self.wei_thread_copy_ndim = len(wei_thread_copy_index)
        assert self.in_thread_copy_ndim in (0, 1, 2)
        assert self.wei_thread_copy_ndim in (0, 1, 2)


        self.coalescing_store_groups = igemm_next_pow2(self.tunable.coalescing_store_groups)
        if self.tunable.fma_type != IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS:
            assert (self.tunable.gemm_m_per_thread * self.tunable.gemm_m_repeat) % self.coalescing_store_groups == 0, \
                f"coalescing store groups should be divided by thread m {self.tunable.gemm_m_per_thread}x{self.tunable.gemm_m_repeat}"

            ctrl_thread_mapping = ctrl_thread_mapping_t()
                    #                        ->      MR x  NR x ML1 x NL1 x ML0 x NL0
            ctrl_thread_mapping.thread_lengths = [self.tunable.gemm_m_repeat, self.tunable.gemm_n_repeat, 1, 1, self.tunable.gemm_m_per_thread, self.tunable.gemm_n_per_thread]
            ctrl_thread_mapping.cluster_lengths = [1, 1, self.tunable.gemm_m_level1_cluster, self.tunable.gemm_n_level1_cluster, self.tunable.gemm_m_level0_cluster, self.tunable.gemm_n_level0_cluster]
            self.thread_mapping = igemm_thread_mapping_t(self.mc, ctrl_thread_mapping)

            ctrl_coalescing_store = ctrl_coalescing_store_t()
            ctrl_coalescing_store.ctm = ctrl_thread_mapping
            ctrl_coalescing_store.coalescing_groups = self.coalescing_store_groups
            ctrl_coalescing_store.data_byte = amdgpu_precision_data_byte(self.tunable.precision)

            ctrl_coalescing_store.vector_write_out = 1                      # TODO: some cases this can be set to other value
            ctrl_coalescing_store.block_size = self.tunable.block_size

            gemm_m_order, gemm_n_order = self.get_lds_gemm_m_gemm_n_order()
            na_c0, na_c1e, na_k0, na_k1, nb_c0, nb_c1e, nb_n0, nb_n1b = self.get_dims_lengths()
            ctrl_coalescing_store.gemm_m_m0_m1 = [na_k0, na_k1]
            if gemm_m_order == IGEMM_FWD_GTC_LDS_STORE_ORDER_GEMM_M_K1_K0:
                ctrl_coalescing_store.gemm_m_order = IGEMM_COALESCING_GEMM_M_ORDER_M1_M0

            ctrl_coalescing_store.adjust_optimal_coalescing_groups()        # in m1_m0 order, must adjust 
            self.coalescing_store = igemm_coalescing_store_t(mc, ctrl_coalescing_store)

        else:
            def flatten(x):
                from functools import reduce
                return reduce(lambda a, b: a*b, x, 1)
            ctrl_xdlops_mapping = get_ctrl_xdlops_mapping_from_wave_tile_fp32(self.tunable.gemm_m_per_block, self.tunable.gemm_n_per_block, self.tunable.wave_tile_m, self.tunable.wave_tile_n, self.tunable.wave_tile_k,
                    self.tunable.wave_repeat_m, self.tunable.wave_repeat_n, self.tunable.wave_step_m, self.tunable.wave_step_n, self.tunable.block_size // AMDGPU_WAVE_SIZE)
            self.xdlops_mapping = igemm_xdlops_mapping_t(self.mc, ctrl_xdlops_mapping)
            assert flatten(ctrl_xdlops_mapping.acc_c_per_thread_m()) % self.coalescing_store_groups == 0, \
                f"coalescing store groups should be divided by agpr per thread in m direction {ctrl_xdlops_mapping.acc_c_per_thread_m()}"

            ctrl_coalescing_store_xdlops = ctrl_coalescing_store_xdlops_t()
            ctrl_coalescing_store_xdlops.cxm = ctrl_xdlops_mapping
            ctrl_coalescing_store_xdlops.coalescing_groups = self.coalescing_store_groups
            ctrl_coalescing_store_xdlops.data_byte = amdgpu_precision_data_byte(self.tunable.precision)

            ctrl_coalescing_store_xdlops.vector_write_out = 1                      # TODO: some cases this can be set to other value
            ctrl_coalescing_store_xdlops.block_size = self.tunable.block_size
        
            # gemm_m_order, gemm_n_order = self.get_lds_gemm_m_gemm_n_order()
            na_nb0, na_nb1, na_e, na_c, nb_k0, nb_k1 = self.get_dims_lengths()
            ctrl_coalescing_store_xdlops.gemm_m_m0_m1 = [na_nb0, na_nb1]
            #if gemm_m_order == IGEMM_FWD_GTC_NHWC_LDS_STORE_ORDER_GEMM_M_N1B_N0:
            #    # we may consider not suppor this mode
            #    ctrl_coalescing_store_xdlops.gemm_m_order = IGEMM_COALESCING_GEMM_M_ORDER_M1_M0
            ctrl_coalescing_store_xdlops.adjust_optimal_coalescing_groups()        # in m1_m0 order, must adjust 
            self.coalescing_store = igemm_coalescing_store_xdlops_t(mc, ctrl_coalescing_store_xdlops)


        self.label_out = f"L_{self.name()}_out"
        self.dict_shifted_stride = dict()

        self.karg = self.kernel_karg_t(mc, self)
        self.sgpr = self.kernel_sgpr_t(mc, self)
        self.vgpr = self.kernel_vgpr_t(mc, self)
        if self.tunable.fma_type == IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS:
            self.agpr = self.kernel_agpr_t(mc, self)
    
    def name(self):
        return igemm_gtc_encode_kernel_name(self.tunable)
    
    def try_shift_stride(self, gpr, shifter):
        assert type(gpr) is sym_t
        with self._deferred_context():
            if gpr.label not in self.dict_shifted_stride:
                self.dict_shifted_stride[gpr.label] = gpr
                self._emit(f"s_lshl_b32 s[{gpr()}], s[{gpr()}], {shifter}")
        return self._get_deferred()

    # will not support order, since nhwc fix order is enough
    '''
    def get_lds_gemm_m_gemm_n_order(self):
        def need_reverse_order(x0, x1):
            if x0 != 1 and x1 == 1:
                return True
            if x0 > x1:
                return True
            return False

        ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = self.get_thread_lengths()

        gemm_n_order = -1   # gemm_n order is not supported

        gemm_m_order = IGEMM_FWD_GTC_NHWC_LDS_STORE_ORDER_GEMM_M_N0_N1B
        if self.tunable.allow_lds_reorder:
            if need_reverse_order(ta_nb0, ta_nb1):
                gemm_m_order = IGEMM_FWD_GTC_NHWC_LDS_STORE_ORDER_GEMM_M_N1B_N0
                assert False, "maybe not correct"

        return gemm_m_order, gemm_n_order
    '''

    class macro_set_flag_hw(macro_base_t):
        def __init__(self, mc, inline = False):
            macro_base_t.__init__(self, mc, inline)
            self.declare_arg("v_flag")
            self.declare_arg("v_ih")
            self.declare_arg("v_iw")
            self.declare_arg("s_h")
            self.declare_arg("s_w")
        def name(self):
            return '.v_fwd_gtc_nhwc_set_flag_hw'

        def expr(self):
            self._emit(f"v_cmp_gt_u32 vcc, s[{self.s_h()}], v[{self.v_ih()}]")
            self._emit(f"v_cndmask_b32 v[{self.v_flag()}], 0, 1, vcc")
            self._emit(f"v_cmp_gt_u32 vcc, s[{self.s_w()}], v[{self.v_iw()}]")
            self._emit(f"v_cndmask_b32 v[{self.v_flag()}], 0, v[{self.v_flag()}], vcc")
    
    class macro_set_flag_nhw(macro_base_t):
        def __init__(self, mc, inline = False):
            macro_base_t.__init__(self, mc, inline)
            self.declare_arg("v_flag")
            self.declare_arg("v_flag_n")
            self.declare_arg("v_ih")
            self.declare_arg("v_iw")
            self.declare_arg("s_h")
            self.declare_arg("s_w")
        def name(self):
            return '.v_fwd_gtc_nhwc_set_flag_nhw'

        def expr(self):
            self._emit(f"v_cmp_gt_u32 vcc, s[{self.s_h()}], v[{self.v_ih()}]")
            self._emit(f"v_cndmask_b32 v[{self.v_flag()}], 0, v[{self.v_flag_n()}], vcc")
            self._emit(f"v_cmp_gt_u32 vcc, s[{self.s_w()}], v[{self.v_iw()}]")
            self._emit(f"v_cndmask_b32 v[{self.v_flag()}], 0, v[{self.v_flag()}], vcc")

    class macro_in_update_hw_t(macro_base_t):
        def __init__(self, mc, inline = False):
            macro_base_t.__init__(self, mc, inline)
            self.declare_arg("v_in_ihi")
            self.declare_arg("v_in_iwi")
            self.declare_arg("v_in_iho")
            self.declare_arg("v_in_iwo")
            self.declare_arg("v_in_iy")
            self.declare_arg("v_in_ix")
            self.declare_arg("s_dilation_h")
            self.declare_arg("s_dilation_w")
        def name(self):
            return '.v_fwd_gtc_nhwc_in_update_hw'

        def expr(self):
            self._emit(f"; ihi = iho * s_stride_h + iy * s_dilation_h - s_pad_h,   here make sure iho <- iho * s_stride_h - s_pad_h before hand")
            self._emit(f"; iwi = iwo * s_stride_w + ix * s_dilation_w - s_pad_w,   here make sure iwo <- iwo * s_stride_w - s_pad_w before hand")
            self._emit(f"v_mad_i32_i24 v[{self.v_in_ihi()}], s[{self.s_dilation_h()}], v[{self.v_in_iy()}], v[{self.v_in_iho()}]")
            self._emit(f"v_mad_i32_i24 v[{self.v_in_iwi()}], s[{self.s_dilation_w()}], v[{self.v_in_ix()}], v[{self.v_in_iwo()}]")

    class macro_in_update_os_t(macro_base_t):
        def __init__(self, mc, data_byte, inline = False):
            macro_base_t.__init__(self, mc, inline)
            self.data_byte = data_byte
            self.declare_arg("v_in_os")
            self.declare_arg("v_in_os_base")
            self.declare_arg("v_in_ihi")
            self.declare_arg("v_in_iwi")
            self.declare_arg("s_wi")
            self.declare_arg("s_in_stride_wi")
            self.declare_arg("v_tmp")
        def name(self):
            return '.v_fwd_gtc_nhwc_in_update_os'

        def expr(self):
            self._emit(f"v_mad_u32_u24 v[{self.v_tmp()}], v[{self.v_in_ihi()}], s[{self.s_wi()}], v[{self.v_in_iwi()}]")
            self._emit(f"v_mul_lo_u32 v[{self.v_tmp()}], s[{self.s_in_stride_wi()}], v[{self.v_tmp()}]")
            self._emit(f"v_add_u32 v[{self.v_in_os()}], v[{self.v_tmp()}], v[{self.v_in_os_base()}]")

    class macro_move_slice_window_k_e1_c_t(macro_base_t):
        '''
        nhwc gemm_k = e*c, and thread/cluster length for e is always 1
        hence always move along c and accumulate into e

        this macro is for input and weight together.
        '''
        def __init__(self, mc, tunable, inline = False):
            macro_base_t.__init__(self, mc, inline)
            self.tunable = tunable
            self.declare_arg("v_move_slice_k_iy")
            self.declare_arg("v_move_slice_k_ix")
            self.declare_arg("v_move_slice_k_ic")
            #self.declare_arg("s_gemm_k_num_y")
            self.declare_arg("s_gemm_k_num_x")
            self.declare_arg("s_gemm_k_num_c")
            self.declare_arg("s_move_slice_k_c")
            self.declare_arg("v_in_os")
            self.declare_arg("v_wei_os")

            # self.declare_arg("s_in_stride_gemm_k_num_c")
            self.declare_arg("s_move_slice_k_in_stride_diff_y")          # indeed stride_y - stride_x, always possitive
            self.declare_arg("s_move_slice_k_in_stride_diff_x")          # indeed stride_x - stride_c, always possitive
            self.declare_arg("s_move_slice_k_stride_c")                  # this is indeed s_move_slice_k_c * data_byte, same for input/weight

            self.declare_arg("v_in_ihi")                    # need update
            self.declare_arg("v_in_iwi")                    # need update
            # self.declare_arg("s_dilation_h")
            # self.declare_arg("s_dilation_w")
            self.declare_arg("s_in_diff_hi")                # s_dilation_h
            self.declare_arg("s_in_diff_wi")                # s_dilation_w
            self.declare_arg("s_in_diff_sub_wi")            # total wi needed to be deduced from iwi, when carry-on

        def name(self):
            return '.v_fwd_gtc_nhwc_move_slice_window_k_e1_c'

        def expr(self):
            self._emit(f"v_add_u32 v[{self.v_move_slice_k_ic()}], s[{self.s_move_slice_k_c()}], v[{self.v_move_slice_k_ic()}]")
            self._emit(f"v_add_u32 v[{self.v_in_os()}], s[{self.s_move_slice_k_stride_c()}], v[{self.v_in_os()}]")
            self._emit(f"v_add_u32 v[{self.v_wei_os()}], s[{self.s_move_slice_k_stride_c()}], v[{self.v_wei_os()}]")     # weight offset always increase, treat y*x*c as single dimension
            self._emit(f"v_cmpx_le_u32 vcc, s[{self.s_gemm_k_num_c()}], v[{self.v_move_slice_k_ic()}]")
            self._emit(f"v_subrev_u32 v[{self.v_move_slice_k_ic()}], s[{self.s_gemm_k_num_c()}], v[{self.v_move_slice_k_ic()}]")
            self._emit(f"v_add_u32 v[{self.v_move_slice_k_ix()}], 1, v[{self.v_move_slice_k_ix()}]")
            self._emit(f"v_add_u32 v[{self.v_in_os()}], s[{self.s_move_slice_k_in_stride_diff_x()}], v[{self.v_in_os()}]")    # merge with above c
            self._emit(f"v_add_u32 v[{self.v_in_iwi()}], s[{self.s_in_diff_wi()}],  v[{self.v_in_iwi()}]")
            self._emit(f"s_mov_b64 exec, -1")
            self._emit_empty_line()
            self._emit(f"v_cmpx_le_u32 vcc s[{self.s_gemm_k_num_x()}], v[{self.v_move_slice_k_ix()}]")
            self._emit(f"v_add_u32 v[{self.v_move_slice_k_iy()}], 1, v[{self.v_move_slice_k_iy()}]")
            self._emit(f"v_add_u32 v[{self.v_in_os()}], s[{self.s_move_slice_k_in_stride_diff_y()}], v[{self.v_in_os()}]")
            self._emit(f"v_subrev_u32 v[{self.v_in_iwi()}], s[{self.s_in_diff_sub_wi()}], v[{self.v_in_iwi()}]")
            self._emit(f"v_add_u32 v[{self.v_in_ihi()}], s[{self.s_in_diff_hi()}],  v[{self.v_in_ihi()}]")
            self._emit(f"s_mov_b64 exec, -1")
            self._emit_empty_line()
            # free of last dim check

    class macro_move_slice_window_k_nxe0_c_t(macro_base_t):
        '''
        used for nxe=0. only c move is needed
        '''
        def __init__(self, mc, tunable, inline = False):
            macro_base_t.__init__(self, mc, inline)
            self.tunable = tunable
            self.declare_arg("v_in_os")
            self.declare_arg("v_wei_os")
            self.declare_arg("s_move_slice_k_stride_c")               # this is indeed s_move_slice_k_c * data_byte

        def name(self):
            return '.v_fwd_gtc_nhwc_move_slice_window_k_nxe0_c'

        def expr(self):
            self._emit(f"v_add_u32 v[{self.v_in_os()}], s[{self.s_move_slice_k_stride_c()}], v[{self.v_in_os()}]")
            self._emit(f"v_add_u32 v[{self.v_wei_os()}], s[{self.s_move_slice_k_stride_c()}], v[{self.v_wei_os()}]")
            self._emit_empty_line()

    class global_load_in_t(mc_base_t):
        def __init__(self, mc, outer):
            mc_base_t.__init__(self, mc)
            self.outer = outer
        def get_issues(self):
            m_wei_2d_global_load, m_in_2d_global_load = outer.get_macro_global_load()
            return m_in_2d_global_load.get_issues()

        def __call__(self):
            s = self.outer.sgpr
            v = self.outer.vgpr

            m_wei_2d_global_load, m_in_2d_global_load = self.outer.get_macro_global_load()
            with self._deferred_context():
                self._emit(f"; load input")
                if self.outer.tunable.nxe != 0:
                    self._emit(f".v_clear_nc {v.v_gld_a()}, {m_in_2d_global_load.ctrl.length_d0 * m_in_2d_global_load.ctrl.length_d1}")
                    self._emit(m_in_2d_global_load(v.v_gld_a(), s.s_p_in(), v.v_in_os(), v.v_in_flag()))
                else:
                    self._emit(m_in_2d_global_load(v.v_gld_a(), s.s_p_in(), v.v_in_os(), None))

            return self._get_deferred()

    class global_load_wei_t(mc_base_t):
        def __init__(self, mc, outer):
            mc_base_t.__init__(self, mc)
            self.outer = outer
        def get_issues(self):
            m_wei_2d_global_load, m_in_2d_global_load  = self.outer.get_macro_global_load()
            return m_wei_2d_global_load.get_issues()
        
        def __call__(self):
            s = self.outer.sgpr
            v = self.outer.vgpr

            m_wei_2d_global_load, m_in_2d_global_load = self.outer.get_macro_global_load()
            s_in_stride_d0, s_in_stride_d1, s_wei_stride_d0, s_wei_stride_d1 = self.outer.get_symbol_global_load_s_stride_d0_d1()
            with self._deferred_context():
                self._emit(f"; load weight")
                # self._emit(f".v_clear_nc {v.v_gld_a()}, {m_wei_2d_global_load.ctrl.length_d0 * m_wei_2d_global_load.ctrl.length_d1}")
                if self.outer.tunable.precache_soffset:
                    self._emit(m_wei_2d_global_load(v.v_gld_b(), s.s_p_wei(), v.v_wei_os(), s_wei_stride_d0(), s_wei_stride_d1(), s.s_wei_offset()))
                else:
                    self._emit(m_wei_2d_global_load(v.v_gld_b(), s.s_p_wei(), v.v_wei_os(), s_wei_stride_d0(), s_wei_stride_d1(), s.s_tmp()))
            return self._get_deferred() 

    class shared_store_in_t(mc_base_t):
        def __init__(self, mc, outer):
            mc_base_t.__init__(self, mc)
            self.outer = outer
        def get_issues(self):
            m_in_2d_shared_store, m_wei_2d_shared_store = self.outer.get_macro_shared_store()
            return  m_in_2d_shared_store.get_issues()
        
        def __call__(self):
            s = self.outer.sgpr
            v = self.outer.vgpr
            m_in_2d_shared_store, m_wei_2d_shared_store = self.outer.get_macro_shared_store()
            with self._deferred_context():
                self._emit(m_in_2d_shared_store(v.v_gld_b(), v.v_sst_b_os()))
            return self._get_deferred()

    class shared_store_wei_t(mc_base_t):
        def __init__(self, mc, outer):
            mc_base_t.__init__(self, mc)
            self.outer = outer
        def get_issues(self):
            m_in_2d_shared_store, m_wei_2d_shared_store = self.outer.get_macro_shared_store()
            return m_wei_2d_shared_store.get_issues()
        
        def __call__(self):
            s = self.outer.sgpr
            v = self.outer.vgpr
            m_in_2d_shared_store, m_wei_2d_shared_store = self.outer.get_macro_shared_store()
            with self._deferred_context():
                self._emit(m_wei_2d_shared_store(v.v_gld_a(), v.v_sst_a_os()))
            return self._get_deferred()

    class kernel_karg_t(mc_base_t):
        def __init__(self, mc, outer):
            mc_base_t.__init__(self, mc)
            self.outer = outer
            self.k_p_in       = sym_t('k_p_in'          ,0)
            self.k_p_wei      = sym_t('k_p_wei'         ,8)
            self.k_p_out      = sym_t('k_p_out'         ,16)
            self.k_hi         = sym_t('k_hi'            ,24)
            self.k_wi         = sym_t('k_wi'            ,28)
            self.k_n          = sym_t('k_n'             ,32)
            self.k_k          = sym_t('k_k'             ,36)
            self.k_c          = sym_t('k_c'             ,40)
            self.k_ho         = sym_t('k_ho'            ,44)
            self.k_wo         = sym_t('k_wo'            ,48)
            self.k_stride_h   = sym_t('k_stride_h'      ,52)
            self.k_stride_w   = sym_t('k_stride_w'      ,56)
            self.k_dilation_h = sym_t('k_dilation_h'    ,60)
            self.k_dilation_w = sym_t('k_dilation_w'    ,64)
            self.k_pad_h      = sym_t('k_pad_h'         ,68)
            self.k_pad_w      = sym_t('k_pad_w'         ,72)
            self.k_y          = sym_t('k_y'             ,76)
            self.k_x          = sym_t('k_x'             ,80)
            self.k_group      = sym_t('k_group'         ,84)
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self.k_magic_0      = sym_t('k_magic_0'         ,88)
                self.k_magic_1      = sym_t('k_magic_1'         ,92)
                self.k_magic_2      = sym_t('k_magic_2'         ,96)
                self.k_magic_3      = sym_t('k_magic_3'         ,100)
                self.k_magic_4      = sym_t('k_magic_4'         ,104)
                self.k_magic_5      = sym_t('k_magic_5'         ,108)
                self.k_magic_6      = sym_t('k_magic_6'         ,112)
                self.k_shift_pack_0 = sym_t('k_shift_pack_0'    ,116)
                self.k_shift_pack_1 = sym_t('k_shift_pack_1'    ,120)
                self.k__pack_0      = sym_t('k__pack_0'         ,124)
                self.k_end          = sym_t('k_end'             ,128)
            else:
                self.k_end          = sym_t('k_end'             ,88)

        def get_count(self):
            return self.k_end.value

        def emit(self):
            for k, v in self.__dict__.items():
                if k.startswith('k_'):
                    self._emit(v.declare())

    class kernel_sgpr_t(mc_base_t):
        def __init__(self, mc, outer):
            mc_base_t.__init__(self, mc)
            ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = outer.get_thread_lengths()
            sseq                            = gpr_sequencer_t()
            self.outer                      = outer
            self.s_ka                       = sym_t('s_ka'                      , sseq(2))
            self.s_bx                       = sym_t('s_bx'                      , sseq(2))
            self.s_p_in                     = sym_t('s_p_in'                    , sseq(4))
            self.s_p_wei                    = sym_t('s_p_wei'                   , sseq(4))
            self.s_p_out                    = sym_t('s_p_out'                   , sseq(4))
            self.s_hi                       = sym_t('s_hi'                      , sseq(1))
            self.s_wi                       = sym_t('s_wi'                      , sseq(1))
            self.s_n                        = sym_t('s_n'                       , sseq(1))
            self.s_k                        = sym_t('s_k'                       , sseq(1))    # this is indeed k_per_group
            self.s_c                        = sym_t('s_c'                       , sseq(1))    # this is indeed c_per_group
            if outer.tunable.nxe != 0:
                self.s_ho                   = sym_t('s_ho'                      , sseq(1))
                self.s_wo                   = sym_t('s_wo'                      , sseq(1))
                self.s_stride_h             = sym_t('s_stride_h'                , sseq(1))
                self.s_stride_w             = sym_t('s_stride_w'                , sseq(1))
                self.s_dilation_h           = sym_t('s_dilation_h'              , sseq(1))
                self.s_dilation_w           = sym_t('s_dilation_w'              , sseq(1))
                self.s_pad_h                = sym_t('s_pad_h'                   , sseq(1))
                self.s_pad_w                = sym_t('s_pad_w'                   , sseq(1))
                self.s_y                    = sym_t('s_y'                       , sseq(1))
                self.s_x                    = sym_t('s_x'                       , sseq(1))
            self.s_group                    = sym_t('s_group'                   , sseq(1))

            # stride for in
            # self.s_in_stride_hi             = sym_t('s_in_stride_hi'            , sseq(1))
            self.s_in_stride_wi             = sym_t('s_in_stride_wi'            , sseq(1))
            self.s_in_stride_n              = sym_t('s_in_stride_n'             , sseq(1))

            # stride for wei
            if tb_k0 != 1:
                self.s_wei_stride_k0        = sym_t('s_wei_stride_k0'           , sseq(1))
            self.s_wei_stride_k             = sym_t('s_wei_stride_k'            , sseq(1))
            #if outer.tunable.nxe != 0:
            #    self.s_wei_stride_y         = sym_t('s_wei_stride_y'            , sseq(1))
            self.s_stride_c                 = sym_t('s_stride_c'                , sseq(1))

            # stride for out
            self.s_out_stride_wo            = sym_t('s_out_stride_wo'           , sseq(1))
            self.s_out_stride_n             = sym_t('s_out_stride_n'            , sseq(1))

            self.s_in_stride_c_c1           = sym_t("s_in_stride_c_c1"          , sseq(1))
            self.s_in_stride_c_c0_c1_diff   = sym_t("s_in_stride_c_c0_c1_diff"  , sseq(1))

            self.s_block_gtc_ig             = sym_t("s_block_gtc_ig"            , sseq(1))
            self.s_block_gtc_ik             = sym_t("s_block_gtc_ik"            , sseq(1))
            self.s_block_gtc_inb            = sym_t("s_block_gtc_inb"           , sseq(1))

            # self.s_block_gtc_in0            = sym_t("s_block_gtc_in0"           , sseq(1))
            # self.s_block_gtc_in1b           = sym_t("s_block_gtc_in1b"          , sseq(1))

            self.s_move_slice_k_c1e         = sym_t("s_move_slice_k_c1e"        , sseq(1))
            if outer.tunable.nxe != 0:
                self.s_move_slice_k_c       = sym_t("s_move_slice_k_c"          , sseq(1))
                self.s_move_slice_k_y       = sym_t("s_move_slice_k_y"          , sseq(1))
                self.s_move_slice_k_x       = sym_t("s_move_slice_k_x"          , self.s_block_gtc_ig.value)

            self.s_move_slice_k_stride_c    = sym_t("s_move_slice_k_stride_c"   , sseq(1))
            self.s_in_diff_sub_wi           = sym_t("s_in_diff_sub_wi"          , sseq(1))
            if outer.tunable.nxe != 0:
                self.s_move_slice_k_in_stride_diff_y    = sym_t("s_move_slice_k_in_stride_diff_y"         , sseq(1))
                self.s_move_slice_k_in_stride_diff_x    = sym_t("s_move_slice_k_in_stride_diff_x"         , sseq(1))


            self.s_knum                     = sym_t("s_knum"                    , 3)
            self.s_gemm_k_num_c             = sym_t("s_gemm_k_num_c"            , sseq(1))
            if outer.tunable.nxe != 0:
                self.s_gemm_k_num_y         = sym_t("s_gemm_k_num_y"            , self.s_y.value)
                self.s_gemm_k_num_x         = sym_t("s_gemm_k_num_x"            , self.s_x.value)

            # self.s_move_slice_k_in_stride_diff_y         = sym_t("s_move_slice_k_in_stride_diff_y"        , sseq(1))
            # self.s_move_slice_k_in_stride_diff_x         = sym_t("s_move_slice_k_in_stride_diff_x"        , sseq(1))

            #if outer.tunable.nxe != 0:
            self.s_dim_b                    = sym_t("s_dim_b"                   , sseq(1))
            self.s_dim_m                    = sym_t("s_dim_m"                   , sseq(1))
            self.s_dim_n                    = sym_t("s_dim_n"                   , sseq(1))

            if outer.tunable.nxe != 0:
                self.s_len_h                = sym_t("s_len_h"                   , sseq(1))
                self.s_len_w                = sym_t("s_len_w"                   , sseq(1))
                self.s_lim_h                = sym_t("s_lim_h"                   , sseq(1))      # used to compare ih, will increase while y increase
                self.s_lim_w                = sym_t("s_lim_w"                   , sseq(1))      # used to compare iw, will increase while x increase
            else:
                self.s_len_h                = sym_t("s_len_h"                   , self.s_hi.value)
                self.s_len_w                = sym_t("s_len_w"                   , self.s_wi.value)
                self.s_lim_h                = sym_t("s_lim_h"                   , self.s_hi.value)
                self.s_lim_w                = sym_t("s_lim_w"                   , self.s_wi.value)

            self.s_thread_stride_w          = sym_t("s_thread_stride_w"         , sseq(1))
            self.s_thread_stride_h          = sym_t("s_thread_stride_h"         , sseq(1))
            self.s_thread_stride_n          = sym_t("s_thread_stride_n"         , sseq(1))

            self.s_kitr                     = sym_t("s_kitr"                    , 1)
            if outer.tunable.precache_soffset:
                m_wei_2d_global_load, m_in_2d_global_load         = outer.get_macro_global_load()
                #in_npc = m_in_2d_global_load.get_num_precache_soffset()
                wei_npc = m_wei_2d_global_load.get_num_precache_soffset()
                #self.s_in_offset           = sym_t("s_in_offset"              ,sseq(in_npc))   # if this number is zero, it is also OK, since we would not use
                self.s_wei_offset          = sym_t("s_wei_offset"             ,sseq(wei_npc))
            # self.s_k_padded                = sym_t("s_k_padded"             ,sseq(1))

            # TODO: this sgpr allocation is a mess
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                # allocate several sgpr to hold magic/shift value.
                self.s_shift_pack_0        = sym_t("s_shift_pack_0"           ,self.s_p_out.value + 2)
                self.s_shift_pack_1        = sym_t("s_shift_pack_1"           ,self.s_p_out.value + 3)

                self.s_magic_2             = sym_t("s_magic_2"                ,self.s_in_stride_c_c1.value)    # when load, loadx4 with magic_0/1
                self.s_magic_3             = sym_t("s_magic_3"                ,self.s_in_stride_c_c0_c1_diff.value) # when load, loadx4 with magic_0/1

                self.s_magic_4             = sym_t("s_magic_4"                ,self.s_move_slice_k_c1e.value)
                self.s_magic_5             = sym_t("s_magic_5"                ,self.s_gemm_k_num_c1.value)
                self.s_magic_6             = sym_t("s_magic_6"                ,self.s_block_gtc_in0.value)

            self.s_tmp                     = sym_t("s_tmp"                    ,sseq(6, 2))
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self.s_magic_0             = sym_t("s_magic_0"                ,self.s_p_wei.value + 2)
                self.s_magic_1             = sym_t("s_magic_1"                ,self.s_p_wei.value + 3)

            self.s_end                     = sym_t("s_end"                    ,sseq())

        def get_count(self):
            return self.s_end.value

        def emit(self):
            assert self.s_end.value <= amdgpu_sgpr_limit(self.mc.arch_config.arch), f"s_end:{self.s_end.value}, tunable:{self.outer.tunable.serialize()}"
            for k, v in self.__dict__.items():
                if k.startswith('s_'):
                    self._emit(v.declare())

    class kernel_vgpr_t(mc_base_t):
        def __init__(self, mc, outer):
            mc_base_t.__init__(self, mc)
            self.outer = outer
            ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = outer.get_thread_lengths()
            ca_nb0, ca_nb1, ca_e, ca_c, cb_k0, cb_k1 = outer.get_cluster_lengths()

            nb_per_thread = ta_nb0 if ta_nb0 != 1 else ta_nb1

            is_vgpr_acc_c = outer.tunable.fma_type != IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS
            vseq = gpr_sequencer_t()
            if is_vgpr_acc_c:
                self.v_c                = sym_t("v_c"            ,vseq(outer.tunable.num_vgpr_accumulate_c))
                v_c_num                 = vseq()
            else:
                v_c_resuable_num        = outer.tunable.num_vgpr_accumulate_a + outer.tunable.num_vgpr_accumulate_b + \
                                            outer.tunable.num_vgpr_global_load_a + outer.tunable.num_vgpr_global_load_b + \
                                            16       # from v_sst_a_os to v_co_sst
                v_c_coalescing_num      = outer.tunable.num_agpr_accumulate_c // outer.coalescing_store_groups
                v_c_needed              = (v_c_coalescing_num - v_c_resuable_num) if (v_c_coalescing_num - v_c_resuable_num) > 0 else 0

                v_c_needed              = v_c_needed if v_c_needed > 2 else 2  # let at least 2
                self.v_c                = sym_t("v_c"            ,vseq(v_c_needed), f"coalescing:{v_c_coalescing_num}, needed:{v_c_needed}, resuable:{v_c_resuable_num}")

            self.v_a                    = sym_t("v_a"               ,vseq(outer.tunable.num_vgpr_accumulate_a))
            self.v_b                    = sym_t("v_b"               ,vseq(outer.tunable.num_vgpr_accumulate_b))
            self.v_gld_a                = sym_t("v_gld_a"           ,vseq(outer.tunable.num_vgpr_global_load_a))
            self.v_gld_b                = sym_t("v_gld_b"           ,vseq(outer.tunable.num_vgpr_global_load_b))
            self.v_sst_a_os             = sym_t("v_sst_a_os"        ,vseq(1))
            self.v_sst_b_os             = sym_t("v_sst_b_os"        ,vseq(1))
            self.v_sld_a_os             = sym_t("v_sld_a_os"        ,vseq(1))
            self.v_sld_b_os             = sym_t("v_sld_b_os"        ,vseq(1))
            
            # self.v_in_os_base           = sym_t("v_in_os_base"      ,vseq(1))
            self.v_in_os                = sym_t("v_in_os"           ,vseq(nb_per_thread))
            if outer.tunable.nxe != 0:
                self.v_in_flag          = sym_t("v_in_flag"         ,vseq(nb_per_thread))

            self.v_wei_os               = sym_t("v_wei_os"          ,vseq(1))

            self.v_gtc_ic               = sym_t("v_gtc_ic"          ,vseq(1))
            #if ca_nb0 != 1:
            #    self.v_gtc_ta_in0       = sym_t("v_gtc_ta_in0"      ,vseq(1))
            self.v_in_inb               = sym_t("v_in_inb"     ,vseq(1))
            #self.v_gtc_ta_in1            = sym_t("v_gtc_ta_in1"      ,vseq(1))
            
            self.v_flag_n               = sym_t("v_flag_n"          ,vseq(1))   # this flag will not change while move_slice_window

            # if tb_k0 != 1:
            #     self.v_wei_ik0          = sym_t("v_wei_ik0"      ,vseq(1))
            self.v_wei_ik               = sym_t("v_wei_ik"       ,vseq(1))

            self.v_co_sst               = sym_t("v_co_sst"          ,vseq(1))
            self.v_co_sld               = sym_t("v_co_sld"          ,vseq(1))

            self.v_out_os               = sym_t("v_out_os"          ,vseq(1))
            if outer.tunable.nxe != 0:
                self.v_out_flag         = sym_t("v_out_flag"        ,vseq(1))
            self.v_out_in0              = sym_t("v_out_in0"         ,vseq(1))
            self.v_out_in1b             = sym_t("v_out_in1b"        ,vseq(1))
            self.v_out_in1              = sym_t("v_out_in1"         ,vseq(1))

            self.v_in_iho               = sym_t("v_in_iho"          ,vseq(1))
            self.v_in_iwo               = sym_t("v_in_iwo"          ,vseq(1))
            self.v_in_ihi               = sym_t("v_in_ihi"          ,vseq(1))
            self.v_in_iwi               = sym_t("v_in_iwi"          ,vseq(1))
            self.v_in_in                = sym_t("v_in_in"           ,vseq(1))

            if outer.tunable.nxe != 0:
                self.v_in_iy            = sym_t("v_in_iy"     ,vseq(1))
                self.v_in_ix            = sym_t("v_in_ix"     ,vseq(1))

            self.v_move_slice_k_ic      = sym_t("v_move_slice_k_ic1" , self.v_gtc_ic.value)
            if outer.tunable.nxe != 0:
                self.v_move_slice_k_iy  = sym_t("v_move_slice_k_iy", self.v_in_iy.value)
                self.v_move_slice_k_ix  = sym_t("v_move_slice_k_ix", self.v_in_ix.value)

            self.v_gemm_in              = sym_t("v_gemm_in"      , vseq(1))
            self.v_gemm_im              = sym_t("v_gemm_im"      , vseq(1))

            self.v_out_iho              = sym_t("v_out_iho" ,vseq(1))
            self.v_out_iwo              = sym_t("v_out_iwo" ,vseq(1))
            self.v_co_sub_m_index       = sym_t("v_co_sub_m_index" ,vseq(1))
            self.v_co_sub_n_index       = sym_t("v_co_sub_n_index" ,vseq(1))

            self.v_cur_k                = sym_t("v_cur_k" ,vseq(1))

            self.v_tmp                  = sym_t("v_tmp"          ,vseq(6, 2))
            total_vgpr                  = vseq()
            if outer.tunable.fma_type == IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS:
                # if xdlops agpr is larger than vgpr usage, must change vgpr count to agpr
                total_vgpr              = max(total_vgpr, outer.tunable.num_agpr_accumulate_c)
            self.v_end                  = sym_t("v_end"          ,total_vgpr)

        def get_count(self):
            return self.v_end.value

        def emit(self):
            for k, v in self.__dict__.items():
                if k.startswith('v_'):
                    self._emit(v.declare())

    class kernel_agpr_t(mc_base_t):
        def __init__(self, mc, outer):
            mc_base_t.__init__(self, mc)
            assert outer.tunable.fma_type == IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS, 'only xdlops can use agpr'
            self.outer         = outer
            aseq = gpr_sequencer_t()
            self.a_c           = sym_t("a_c",          aseq(outer.tunable.num_agpr_accumulate_c))
            self.a_end         = sym_t("a_end",        aseq())

        def get_count(self):
            return self.a_end.value

        def emit(self):
            for k, v in self.__dict__.items():
                if k.startswith('a_'):
                    self._emit(v.declare())

    def get_thread_lengths(self):
        t_ta = self.tunable.tensor_a_thread_lengths
        t_tb = self.tunable.tensor_b_thread_lengths

        assert len(t_ta) == 4 and len(t_tb) == 4

        ta_e, ta_c, ta_nb0, ta_nb1 = t_ta[0], t_ta[1], t_ta[2], t_ta[3]
        tb_e, tb_c, tb_k0,  tb_k1  = t_tb[0], t_tb[1], t_tb[2], t_tb[3]

        assert ta_e == tb_e and ta_c == tb_c

        assert ta_e == 1, "currently not support >1 in e dimension"

        # it's no point to have both x0, x1 have copy value
        assert ta_nb0 != 1 and ta_nb1 != 1
        assert tb_k0 != 1 and tb_k1 != 1

        return ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1  # M, K, N

    def get_cluster_lengths(self):
        c_ta = self.tunable.tensor_a_cluster_lengths
        c_tb = self.tunable.tensor_b_cluster_lengths

        assert len(c_ta) == 4 and len(c_tb) == 4

        ca_e, ca_c, ca_nb0, ca_nb1 = c_ta[0], c_ta[1], c_ta[2], c_ta[3]
        cb_e, cb_c, cb_k0,  cb_k1  = c_tb[0], c_tb[1], c_tb[2], c_tb[3]

        assert ca_nb1 != 1
        assert ca_e == cb_e and ca_c == cb_c

        assert ca_e == 1 and ca_nb0 == 1 and cb_k0 == 1

        return ca_nb0, ca_nb1, ca_e, ca_c, cb_k0, cb_k1  # M, K, N

    def get_dims_lengths(self):
        ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = self.get_thread_lengths()
        ca_nb0, ca_nb1, ca_e, ca_c, cb_k0, cb_k1 = self.get_cluster_lengths()

        na_nb0, na_nb1, na_e, na_c = ta_nb0 * ca_nb0, ta_nb1 * ca_nb1, ta_e * ca_e, ta_c * ca_c
        nb_k0, nb_k1               = tb_k0  * cb_k0, tb_k1  * cb_k1

        return na_nb0, na_nb1, na_e, na_c, nb_k0, nb_k1  # M, K, N

    def get_thread_copy_dims(self):
        ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = self.get_thread_lengths()
        in_thread_copy_dims  = [ta_nb0, ta_nb1, ta_e, ta_c]
        wei_thread_copy_dims = [tb_k0,  tb_k1,  ta_e, ta_c]     # always reordered!
        return in_thread_copy_dims, wei_thread_copy_dims

    def get_thread_copy_index(self):
        in_thread_copy_dims, wei_thread_copy_dims = self.get_thread_copy_dims()
        in_thread_copy_index  = _find_non_1_index_in_list(in_thread_copy_dims)
        wei_thread_copy_index = _find_non_1_index_in_list(wei_thread_copy_dims)

        '''
        if thread lengths both dimension is 1, means every thread only copy one pixel.
        we need support this also
        '''
        return in_thread_copy_index, wei_thread_copy_index

    def get_macro_global_load(self):
        '''
        NOTICE: input/wei always load gemm_k (e*c) first. indeed always load c, and do vector load if possible
        '''
        inline = True if self.tunable.fma_interleave else False
        ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = self.get_thread_lengths()
        na_nb0, na_nb1, na_e, na_c, nb_k0, nb_k1 = self.get_dims_lengths()

        in_thread_copy_dims, wei_thread_copy_dims = self.get_thread_copy_dims()
        in_thread_copy_index, wei_thread_copy_index = self.get_thread_copy_index()
        ctrl_wei_gld = ctrl_2d_global_load_t()
        ctrl_in_gld = ctrl_2d_global_load_t()

        ctrl_wei_gld.vector_d1 = utility_gcd(ta_c, 4) if ta_c != 1 else 1
        ctrl_in_gld.vector_d1  = utility_gcd(ta_c, 4) if ta_c != 1 else 1

        if self.wei_thread_copy_ndim == 2:
            ctrl_wei_gld.length_d0 = wei_thread_copy_dims[wei_thread_copy_index[0]]
            ctrl_wei_gld.length_d1 = wei_thread_copy_dims[wei_thread_copy_index[1]]
        elif self.wei_thread_copy_ndim == 1:
            ctrl_wei_gld.length_d0 = 1
            ctrl_wei_gld.length_d1 = wei_thread_copy_dims[wei_thread_copy_index[0]]
        else:
            assert False

        if self.in_thread_copy_ndim == 2:
            ctrl_in_gld.length_d0 = in_thread_copy_dims[in_thread_copy_index[0]]
            ctrl_in_gld.length_d1 = in_thread_copy_dims[in_thread_copy_index[1]]
        elif self.in_thread_copy_ndim == 1:
            ctrl_in_gld.length_d0 = 1
            ctrl_in_gld.length_d1 = in_thread_copy_dims[in_thread_copy_index[0]]
        else:
            assert False

        if self.tunable.precache_soffset:
            return macro_igemm_2d_global_load_precache_soffset_t(self.mc, ctrl_wei_gld, inline), \
                    macro_igemm_2d_global_load_precache_voffset_t(self.mc, ctrl_in_gld, inline)
        else:
            return macro_igemm_2d_global_load_t(self.mc, ctrl_wei_gld, inline),  macro_igemm_2d_global_load_precache_voffset_t(self.mc, ctrl_in_gld, inline)

    def get_macro_shared_store(self):
        #in_thread_copy_dims, wei_thread_copy_dims = self.get_thread_copy_dims()
        #in_thread_copy_index, wei_thread_copy_index = self.get_thread_copy_index()
        na_nb0, na_nb1, na_e, na_c, nb_k0, nb_k1 = self.get_dims_lengths()
        ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = self.get_thread_lengths()
        data_byte = amdgpu_precision_data_byte(self.tunable.precision)

        k_pack = ta_c   # always use this as k_pack

        # input is gemm_k * gemm_m * k_pack
        in_sst_ctrl = ctrl_3d_shared_store_t()
        in_sst_ctrl.length_d0 = ta_nb0
        in_sst_ctrl.length_d1 = ta_nb1
        in_sst_ctrl.length_dp = k_pack
        in_sst_ctrl.stride_d0 = na_nb1 * k_pack * data_byte
        in_sst_ctrl.stride_d1 = k_pack * data_byte

        # wei is gemm_k * gemm_n * k_pack
        wei_sst_ctrl = ctrl_3d_shared_store_t()
        wei_sst_ctrl.length_d0 = tb_k0
        wei_sst_ctrl.length_d1 = tb_k1
        wei_sst_ctrl.length_dp = k_pack
        wei_sst_ctrl.stride_d0 = nb_k1 * k_pack * data_byte
        wei_sst_ctrl.stride_d1 = k_pack * data_byte

        inline = True if self.tunable.fma_interleave else False 
        return macro_igemm_3d_shared_store_t(self.mc, in_sst_ctrl, inline), macro_igemm_3d_shared_store_t(self.mc, wei_sst_ctrl, inline)

    # computation macro
    def get_macro_in_update_hw(self):
        inline = True if self.tunable.fma_interleave else False
        return self.macro_in_update_hw_t(self.mc, inline)

    def get_macro_in_update_os(self):
        inline = True if self.tunable.fma_interleave else False
        return self.macro_in_update_os_t(self.mc, amdgpu_precision_data_byte(self.tunable.precision), inline)

    def get_macro_move_slice_window(self):
        inline = True if self.tunable.fma_interleave else False
        if self.tunable.nxe != 0:
            move_slice_window = self.macro_move_slice_window_k_e1_c_t(self.mc, self.tunable, inline)
        else:
            move_slice_window = self.macro_move_slice_window_k_nxe0_c_t(self.mc, self.tunable, inline)

        # return single functor !
        return move_slice_window

    def get_macro_set_flag_hw(self):
        inline = True if self.tunable.fma_interleave else False
        return self.macro_set_flag_hw(self.mc, inline)

    def get_macro_set_flag_nhw(self):
        inline = True if self.tunable.fma_interleave else False
        return self.macro_set_flag_nhw(self.mc, inline)

    def get_symbol_global_load_s_stride_d0_d1(self):
        ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = self.get_thread_lengths()
        # get the symbol object that load 2d may use
        s = self.sgpr
        s_dummy = sym_t("s_dummy")
        in_thread_copy_index, wei_thread_copy_index = self.get_thread_copy_index()

        # input is ignored
        # [ta_nb0, ta_nb1, ta_e, ta_c]
        in_stride_gprs = [s_dummy,
                            s_dummy,
                            s_dummy,
                            s.s_stride_c]

        # [tb_k0, tb_k1, ta_e, ta_c]
        wei_stride_gprs = [s.s_wei_stride_k0 if tb_k0 != 1 else s_dummy,
                            s.s_wei_stride_k if tb_k1 != 1 else s_dummy,
                            s_dummy,
                            s.s_stride_c]

        if self.in_thread_copy_ndim == 2:
            s_in_stride_d0 = in_stride_gprs[in_thread_copy_index[0]]
            s_in_stride_d1 = in_stride_gprs[in_thread_copy_index[1]]
        elif self.in_thread_copy_ndim == 1:
            s_in_stride_d0 = s_dummy
            s_in_stride_d1 = in_stride_gprs[in_thread_copy_index[0]]
        else:
            assert False

        if self.wei_thread_copy_ndim == 2:
            # print(f" ____ wei_thread_copy_index:{len(wei_thread_copy_index)}, {wei_thread_copy_index}")
            s_wei_stride_d0 = wei_stride_gprs[wei_thread_copy_index[0]]
            s_wei_stride_d1 = wei_stride_gprs[wei_thread_copy_index[1]]
        elif self.wei_thread_copy_ndim == 1:
            s_wei_stride_d0 = s_dummy
            s_wei_stride_d1 = wei_stride_gprs[wei_thread_copy_index[0]]
        else:
            assert False

        return s_in_stride_d0, s_in_stride_d1, s_wei_stride_d0, s_wei_stride_d1

    def get_kernel_code(self):
        kernel_code = amdgpu_kernel_code_t({
                'enable_sgpr_kernarg_segment_ptr'   :   1,
                'enable_sgpr_workgroup_id_x'        :   1,
                'enable_vgpr_workitem_id'           :   0,
                'workgroup_group_segment_byte_size' :   self.tunable.lds_total,
                'kernarg_segment_byte_size'         :   self.karg.get_count(),
                'wavefront_sgpr_count'              :   self.sgpr.get_count() + 2*3,
                'workitem_vgpr_count'               :   self.vgpr.get_count()
                })
        return kernel_code

    def get_kernel_args(self):
        '''
        float *p_in;
        float *p_wei;
        float *p_out;
        int hi;
        int wi;
        int n;
        int k;
        int c;
        int ho;
        int wo;
        int stride_h;
        int stride_w;
        int dilation_h;
        int dilation_w;
        int pad_h;
        int pad_w;
        int y;
        int x;
        int group;
        /* if use magic division */
        uint32_t magic_0;           // denom: sa=0: n*b / n_per_block, sa=1: k / m_per_block
        uint32_t magic_1;           // denom: ((n / nb_n0) * b) / nb_n1b
        uint32_t magic_2;           // denom: y*x, if nxe==0 not used
        uint32_t magic_3;           // denom: x, if nxe==0 not used
        uint32_t magic_4;           // denom: b
        uint32_t magic_5;           // denom: wo
        uint32_t magic_6;           // denom: n*b*k / (m_per_block*n_per_block)
        uint32_t shift_pack_0;
        uint32_t shift_pack_1;
        uint32_t __pack_0;
        '''
        kas = []
        # name: {}, .size: {}, .offset: {}, .value_kind: {}, .value_type
        kas.append(amdgpu_kernel_arg_t('p_in'           , 8,   0, 'global_buffer','f32',address_space='global',is_const='true'))
        kas.append(amdgpu_kernel_arg_t('p_wei'          , 8,   8, 'global_buffer','f32',address_space='global',is_const='true'))
        kas.append(amdgpu_kernel_arg_t('p_out'          , 8,  16, 'global_buffer','f32',address_space='global',is_const='false'))
        kas.append(amdgpu_kernel_arg_t('hi'             , 4,  24, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('wi'             , 4,  28, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('n'              , 4,  32, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('k'              , 4,  36, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('c'              , 4,  40, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('ho'             , 4,  44, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('wo'             , 4,  48, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('stride_h'       , 4,  52, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('stride_w'       , 4,  56, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('dilation_h'     , 4,  60, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('dilation_w'     , 4,  64, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('pad_h'          , 4,  68, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('pad_w'          , 4,  72, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('y'              , 4,  76, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('x'              , 4,  80, 'by_value','i32'))
        kas.append(amdgpu_kernel_arg_t('group'          , 4,  84, 'by_value','i32'))
        if IGEMM_GTC_FEAT_MAGIC_DIVISION:
            kas.append(amdgpu_kernel_arg_t('magic_0'        , 4,  88, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('magic_1'        , 4,  92, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('magic_2'        , 4,  96, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('magic_3'        , 4, 100, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('magic_4'        , 4, 104, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('magic_5'        , 4, 108, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('magic_6'        , 4, 112, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('shift_pack_0'   , 4, 116, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('shift_pack_1'   , 4, 120, 'by_value','i32'))
            kas.append(amdgpu_kernel_arg_t('__pack_0'       , 4, 124, 'by_value','i32'))
        else:
            pass
        return kas

    def get_kernel_info(self):
        kernel_code = self.get_kernel_code()
        kernel_args = self.get_kernel_args()
        kernel_info = amdgpu_kernel_info_t(kernel_code, self.name(), self.tunable.block_size, kernel_args)
        return kernel_info

    def get_kernel_macros(self):
        kernel_macros = []
        for attrs in dir(self):
            if attrs.startswith('get_macro_'):
                functor = getattr(self, attrs)
                rtn = functor()
                if rtn is None:
                    continue

                # here we follow the convention in code:
                # #1. for macro like emit class, use emit() to generate macro definition, use __call__() to call this macro
                # #2. for non-macro like emit class, which might want to "inline-ed" into normal code, no emit() is defined, just __call__().
                # hence need to check if has attr name "emit". if not have, it is type #2, no need to do emit() before hand.
                if type(rtn) is tuple:
                    for e in rtn:
                        #if hasattr(e, 'emit'):
                        if not e.is_inline():
                            #continue
                            kernel_macros.extend([m for m in rtn])
                else:
                    #if hasattr(rtn, 'emit'):
                    if not e.is_inline():
                        #continue
                        kernel_macros.append(rtn)
        return kernel_macros

    def emit_kernel_prologue(self):
        s = self.sgpr
        v = self.vgpr
        k = self.karg

        ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = self.get_thread_lengths()
        ca_nb0, ca_nb1, ca_e, ca_c, cb_k0, cb_k1 = self.get_cluster_lengths()
        na_nb0, na_nb1, na_e, na_c, nb_k0, nb_k1 = self.get_dims_lengths()

        data_byte = amdgpu_precision_data_byte(self.tunable.precision)

        m_in_update_hw = self.get_macro_in_update_hw()
        m_in_update_os = self.get_macro_in_update_os()

        m_set_flag_hw       = self.get_macro_set_flag_hw()
        m_set_flag_nhw      = self.get_macro_set_flag_nhw()
        s_in_stride_d0, s_in_stride_d1, s_wei_stride_d0, s_wei_stride_d1 = self.get_symbol_global_load_s_stride_d0_d1()

        m_wei_2d_global_load, m_in_2d_global_load = self.get_macro_global_load()

        tc_index_dispatcher = igemm_thread_cluster_index_dispatcher_t(self.mc)
        tc_index_accumulator = igemm_thread_cluster_index_accumulator_t(self.mc)


        if IGEMM_GTC_FEAT_MAGIC_DIVISION:
            m_mdiv_u32_vs = macro_mdiv_u32_rem_vs_t(self.mc)
            m_mdiv_u32_ss = macro_mdiv_u32_rem_ss_t(self.mc)
        else:
            m_int_div_rem_vv = macro_int_div_rem_vv_t(self.mc)
            m_int_div_rem_vs = macro_int_div_rem_vs_t(self.mc)
            m_int_div_rem_ss = macro_int_div_rem_ss_t(self.mc)

        s_dummy = sym_t("s_dummy")

        # start emit
        self._emit(f"s_load_dwordx2  s[{s.s_p_in((0,1))}],    s[{s.s_ka((0, 1))}],    0+{k.k_p_in()}")
        self._emit(f"s_load_dwordx2  s[{s.s_p_wei((0,1))}],   s[{s.s_ka((0, 1))}],    0+{k.k_p_wei()}")
        self._emit(f"s_load_dwordx2  s[{s.s_p_out((0,1))}],   s[{s.s_ka((0, 1))}],    0+{k.k_p_out()}")
        if self.tunable.nxe != 0:
            self._emit(f"s_load_dwordx8 s[{s.s_hi((0, 7))}],    s[{s.s_ka((0, 1))}],    0+{k.k_hi()}")
            self._emit(f"s_load_dwordx8 s[{s.s_stride_w((0, 7))}],    s[{s.s_ka((0, 1))}],    0+{k.k_stride_w()}")
        else:
            self._emit(f"s_load_dwordx4 s[{s.s_hi((0, 3))}],    s[{s.s_ka((0, 1))}],    0+{k.k_hi()}")
            self._emit(f"s_load_dword s[{s.s_c()}],    s[{s.s_ka((0, 1))}],    0+{k.k_c()}")
            self._emit(f"s_load_dword s[{s.s_group()}],    s[{s.s_ka((0, 1))}],    0+{k.k_group()}")

        if IGEMM_GTC_FEAT_MAGIC_DIVISION:
            self._emit(f"s_load_dwordx2 s[{s.s_magic_0((0, 1))}],  s[{s.s_ka((0, 1))}],  0+{k.k_magic_0()}")
            self._emit(f"s_load_dwordx2 s[{s.s_tmp((2, 3))}],  s[{s.s_ka((0, 1))}],  0+{k.k_magic_2()}")
            self._emit(f"s_load_dwordx2 s[{s.s_tmp((4, 5))}],  s[{s.s_ka((0, 1))}],  0+{k.k_magic_4()}")
            self._emit(f"s_load_dword s[{s.s_magic_6()}],  s[{s.s_ka((0, 1))}],  0+{k.k_magic_6()}")
            self._emit(f"s_load_dwordx2 s[{s.s_shift_pack_0((0, 1))}], s[{s.s_ka((0, 1))}],  0+{k.k_shift_pack_0()}")

        self._emit(f"; in(e, c, nb0, nb1) thread_lengths: {ta_e}x{ta_c}x{ta_nb0}x{ta_nb1}, cluster_length: {ca_e}x{ca_c}x{ca_nb0}x{ca_nb1}")
        self._emit(f"v_mov_b32 v[{v.v_tmp()}], v0")
        self._emit(tc_index_dispatcher(v.v_gtc_ic(),  v.v_tmp(),  ca_c, ta_c))
        self._emit(tc_index_dispatcher(v.v_in_inb(), v.v_tmp(),  ca_nb1, ta_nb1, True))

        self._emit(f"; wei(e, c, k0, k1) thread_length: {ta_e}x{ta_c}x{tb_k0}x{tb_k1}, cluster_length: {ca_e}x{ca_c}x{cb_k0}x{cb_k1}")
        # weight ic same as input
        self._emit(f"v_lshrrev_b32 v[{v.v_tmp()}], {igemm_log2(ca_c)}, v0")
        self._emit(tc_index_dispatcher(v.v_wei_ik(), v.v_tmp(), cb_k, tb_k, True))
        self._emit_empty_line()

        self._emit(f"s_mov_b32 s[{s.s_p_in(2)}], 0xffffffff")
        self._emit(f"s_mov_b32 s[{s.s_p_in(3)}], 0x27000")

        if self.tunable.nxe != 0:
            self._emit(f"v_mov_b32 v[{v.v_in_iy()}], 0")
            self._emit(f"v_mov_b32 v[{v.v_in_ix()}], 0")

        self._emit(f"s_waitcnt lgkmcnt(0)")
        self._emit_empty_line()
        if IGEMM_GTC_FEAT_MAGIC_DIVISION:
            self._emit(f"s_mov_b32 s[{s.s_magic_2()}], s[{s.s_tmp(2)}]")
            self._emit(f"s_mov_b32 s[{s.s_magic_3()}], s[{s.s_tmp(3)}]")
            self._emit(f"s_mov_b32 s[{s.s_magic_4()}], s[{s.s_tmp(4)}]")
            self._emit(f"s_mov_b32 s[{s.s_magic_5()}], s[{s.s_tmp(5)}]")
        self._emit(f"; calculate index")

        # calculate stride, not shift data byte yet
        # input
        self._emit(f"s_mul_i32 s[{s.s_in_stride_wi()}], s[{s.s_c()}], s[{s.s_group()}]")
        self._emit(f"s_mul_i32 s[{s.s_tmp(2)}], s[{s.s_wi()}], s[{s.s_in_stride_wi()}]")
        self._emit(f"s_mul_i32 s[{s.s_in_stride_n()}], s[{s.s_hi()}], s[{s.s_tmp(2)}]")

        # weight
        if self.tunable.nxe != 0:
            self._emit(f"s_mul_i32 s[{s.s_wei_stride_y()}], s[{s.s_x()}], s[{s.s_c()}]")
            self._emit(f"s_mul_i32 s[{s.s_wei_stride_k()}], s[{s.s_wei_stride_y()}], s[{s.s_y()}]")
        else:
            self._emit(f"s_mov_b32 s[{s.s_wei_stride_k()}], s[{s.s_c()}]")
        
        if tb_k0 != 1:
            self._emit(f"s_lshl_b32 s[{s.s_wei_stride_K0()}], s[{s.s_wei_stride_K()}], {igemm_log2(nb_k1)}")

        # output
        self._emit(f"s_mul_i32 s[{s.s_out_stride_wo()}], s[{s.s_k()}], s[{s.s_group()}]")
        self._emit(f"s_mul_i32 s[{s.s_tmp(1)}], s[{s.s_wo() if self.tunable.nxe != 0 else s.s_wi()}], s[{s.s_out_stride_wo()}]")
        self._emit(f"s_mul_i32 s[{s.s_out_stride_n()}], s[{s.s_ho() if self.tunable.nxe != 0 else s.s_hi()}], s[{s.s_tmp(1)}]")

        # TODO: accumulate splited batch here

        # early init s_knum in case shifted
        self._emit(f"s_mov_b32 s[{s.s_knum()}], s[{s.s_wei_stride_k()}]")

        # pad gemm_m, gemm_n
        if self.tunable.nxe != 0:
            self._emit(f"s_mul_i32 s[{s.s_dim_b()}], s[{s.s_ho()}], s[{s.s_wo()}]")
        else:
            self._emit(f"s_mul_i32 s[{s.s_dim_b()}], s[{s.s_hi()}], s[{s.s_wi()}]")

        self._emit(f"s_mul_i32 s[{s.s_tmp(2)}], s[{s.s_n()}], s[{s.s_dim_b()}]")
        self._emit(f"s_add_u32 s[{s.s_tmp()}], {self.tunable.gemm_m_per_block - 1}, s[{s.s_tmp(2)}]")
        self._emit(f"s_lshr_b32 s[{s.s_tmp(1)}], s[{s.s_tmp()}], {igemm_log2(self.tunable.gemm_m_per_block)}")
        self._emit(f"s_lshl_b32 s[{s.s_dim_m()}], s[{s.s_tmp(1)}], {igemm_log2(self.tunable.gemm_m_per_block)}")

        self._emit(f"s_add_u32 s[{s.s_tmp()}], {self.tunable.gemm_n_per_block - 1}, s[{s.s_k()}]")
        self._emit(f"s_lshr_b32 s[{s.s_tmp(1)}], s[{s.s_tmp()}], {igemm_log2(self.tunable.gemm_n_per_block)}")
        self._emit(f"s_lshl_b32 s[{s.s_dim_n()}], s[{s.s_tmp(1)}], {igemm_log2(self.tunable.gemm_n_per_block)}")

        self._emit_empty_line()
        self._emit(f"; gemm_m_per_block:{self.tunable.gemm_m_per_block}, gemm_n_per_block:{self.tunable.gemm_n_per_block}, source_access_order:{self.tunable.source_access_order}")

        # calculate group index
        self._emit(f"s_lshr_b32 s[{s.s_tmp()}], s[{s.s_dim_m()}], {igemm_log2(self.tunable.gemm_m_per_block)}")
        self._emit(f"s_lshr_b32 s[{s.s_tmp(1)}], s[{s.s_dim_n()}], {igemm_log2(self.tunable.gemm_n_per_block)}")
        self._emit(f"s_mul_i32 s[0], s[{s.s_tmp(1)}], s[{s.s_tmp()}]")
        if IGEMM_GTC_FEAT_MAGIC_DIVISION:
            self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080010 ; offset:16, width:8")
            self._emit(m_mdiv_u32_ss(s.s_tmp(4), s.s_block_gtc_ig(), s.s_bx(), s.s_magic_6(), s.s_tmp(3), '0', s.s_tmp()))
        else:
            self._emit(m_int_div_rem_ss(s.s_tmp(4), s.s_block_gtc_ig(), s.s_bx(), '0', v.v_tmp(5), v.v_tmp(), s.s_tmp()))

        # s.s_tmp(4)=> rem, gemm_m, gemm_n, s.s_block_gtc_ig()=> quo, group
        self._emit(f"s_mov_b32 s[{s.s_bx()}], s[{s.s_tmp(4)}]")

        if self.tunable.source_access_order == IGEMM_GTC_TUNABLE_SOURCE_ACCESS_ORDER_GEMM_M_GEMM_N:
            self._emit(f"s_lshr_b32 s[0], s[{s.s_dim_n()}], {igemm_log2(self.tunable.gemm_n_per_block)}")
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_0()}], 0x00080000 ; offset:0, width:8")
                self._emit(m_mdiv_u32_ss(s.s_tmp(4), s.s_tmp(5), s.s_bx(), s.s_magic_0(), s.s_tmp(3), '0', s.s_tmp()))
            else:
                self._emit(m_int_div_rem_ss(s.s_tmp(4), s.s_tmp(5), s.s_bx(), '0', v.v_tmp(5), v.v_tmp(), s.s_tmp()))

        else:
            self._emit(f"s_lshr_b32 s[0], s[{s.s_dim_m()}], {igemm_log2(self.tunable.gemm_m_per_block)}")
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_0()}], 0x00080000 ; offset:0, width:8")
                self._emit(m_mdiv_u32_ss(s.s_tmp(5), s.s_tmp(4), s.s_bx(), s.s_magic_0(), s.s_tmp(3), '0', s.s_tmp()))
            else:
                self._emit(m_int_div_rem_ss(s.s_tmp(5), s.s_tmp(4), s.s_bx(), '0', v.v_tmp(5), v.v_tmp(), s.s_tmp()))

        self._emit(f"; s_tmp+4:block_gtc_in, s_tmp+5:block_gtc_im")
        self._emit(f"s_lshl_b32 s[{s.s_block_gtc_ik()}], s[{s.s_tmp(4)}], {igemm_log2(self.tunable.gemm_n_per_block)}")
        self._emit(f"s_lshl_b32 s[{s.s_block_gtc_inb()}], s[{s.s_tmp(5)}], {igemm_log2(self.tunable.gemm_m_per_block)}")

        # transform nb
        self._emit(f"v_add_u32 v[{v.v_tmp(5)}], s[{s.s_block_gtc_inb()}], v[{v.v_in_inb()}]")
        if self.tunable.nxe != 0:
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080000 ; offset:0, width:8")
                self._emit(m_mdiv_u32_vs(v.v_tmp(4), v.v_in_in(), v.v_tmp(5), s.s_magic_4(), s.s_tmp(3), s.s_dim_b(), v.v_tmp()))
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080008 ; offset:8, width:8")
                self._emit(m_mdiv_u32_vs(v.v_in_iwo(), v.v_in_iho(), v.v_tmp(4), s.s_magic_5(), s.s_tmp(3), s.s_wo(), v.v_tmp()))
            else:
                self._emit(m_int_div_rem_vs(v.v_tmp(4), v.v_in_in(), v.v_tmp(5), s.s_dim_b(), v.v_tmp(), s.s_tmp()))
                self._emit(m_int_div_rem_vs(v.v_in_iwo(), v.v_in_iho(), v.v_tmp(4), s.s_wo(), v.v_tmp(), s.s_tmp()))

            # ihi = iho * s_stride_h + iy * s_dilation_h - s_pad_h
            # iwi = iwo * s_stride_w + ix * s_dilation_w - s_pad_w
            self._emit(f"v_mul_lo_u32 v[{v.v_in_iho()}], s[{s.s_stride_h()}], v[{v.v_in_iho()}]")
            self._emit(f"v_sub_i32 v[{v.v_in_ihi()}], v[{v.v_in_iho()}], s[{s.s_pad_h()}]")
            self._emit(f"v_mul_lo_u32 v[{v.v_in_iwo()}], s[{s.s_stride_w()}], v[{v.v_in_iwo()}]")
            self._emit(f"v_sub_i32 v[{v.v_in_iwi()}], v[{v.v_in_iwo()}], s[{s.s_pad_w()}]")
            self._emit_empty_line()

        else:
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080000 ; offset:0, width:8")
                self._emit(m_mdiv_u32_vs(v.v_tmp(4), v.v_in_in(), v.v_tmp(5), s.s_magic_4(), s.s_tmp(3), s.s_dim_b(), v.v_tmp()))
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080008 ; offset:8, width:8")
                self._emit(m_mdiv_u32_vs(v.v_in_iwi(), v.v_in_ihi(), v.v_tmp(4), s.s_magic_5(), s.s_tmp(3), s.s_wi(), v.v_tmp()))
            else:
                self._emit(m_int_div_rem_vs(v.v_tmp(4), v.v_in_in(), v.v_tmp(5), s.s_dim_b(), v.v_tmp(), s.s_tmp()))
                self._emit(m_int_div_rem_vs(v.v_in_iwi(), v.v_in_ihi(),  v.v_tmp(4), s.s_wi(), v.v_tmp(), s.s_tmp()))
        '''
        from here, need track ihi, iwi in move slice window
        '''

        # update flag for batch size
        self._emit(f"v_cmp_gt_u32 vcc, s[{self.s_n()}], v[{self.v_in_in()}]")
        self._emit(f"v_cndmask_b32 v[{self.v_flag_n()}], 0, 1, vcc")

        self._emit(f"; calculate in offset")
        # compute group distance
        self._emit(f"s_lshl_b32 s[{s.s_block_gtc_ig()}], s[{s.s_block_gtc_ig()}], {igemm_log2(data_byte)}")
        self._emit(f"s_mul_i32 s[{s.s_tmp()}], s[{s.s_block_gtc_ig()}], s[{s.s_c()}]")
        self._emit(f"s_mul_hi_u32 s[{s.s_tmp(1)}], s[{s.s_block_gtc_ig()}], s[{s.s_c()}]")
        self._emit(f"s_add_u32 s[{s.s_p_in()}], s[{s.s_p_in()}], s[{s.s_tmp()}]")
        self._emit(f"s_addc_u32 s[{s.s_p_in(1)}], s[{s.s_p_in(1)}], s[{s.s_tmp(1)}]")
        self._emit_empty_line()

        self._emit(f"v_mul_lo_u32 v[{v.v_tmp(1)}], s[{s.s_in_stride_n()}], v[{v.v_in_in()}]")
        # s_in_stride_wi need shift before!
        self._emit(self.try_shift_stride(s.s_in_stride_wi, igemm_log2(data_byte)))
        
        self._emit(f"v_add_lshl_u32 v[{v.v_tmp(4)}], v[{v.v_gtc_ic()}], v[{v.v_tmp(1)}], {igemm_log2(data_byte)}")
        self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_wi()}], v[{v.v_in_ihi()}]")
        self._emit(f"v_add_u32 v[{v.v_tmp()}], v[{v.v_in_iwi()}], v[{v.v_tmp()}]")
        self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_in_stride_wi()}], v[{v.v_tmp()}]")
        self._emit(f"v_add_u32 v[{v.v_in_os()}], v[{v.v_tmp(4)}], v[{v.v_tmp()}]")
        if self.tunable.nxe != 0:
            self._emit(m_set_flag_nhw(v.v_in_flag(), v.v_flag_n(), v.v_in_ihi(), v.v_in_iwi(), s.s_hi(), s.s_wi()))
        self._emit_empty_line()

        if self.tunable.nxe != 0:
            self._emit(f"s_mul_i32 s[{s.s_len_h()}], s[{s.s_ho()}], s[{s.s_stride_h()}]")
            self._emit(f"s_mul_i32 s[{s.s_len_w()}], s[{s.s_wo()}], s[{s.s_stride_w()}]")
            self._emit(f"s_mov_b32 s[{s.s_lim_h()}], s[{s.s_len_h()}]")
            self._emit(f"s_mov_b32 s[{s.s_lim_w()}], s[{s.s_len_w()}]")

        # voffset
        if ta_nb0 != 1 or ta_nb1 != 1:
            thread_stride = na_nb1 if ta_nb0 != 1 else 1
            self._emit(f"s_mov_b32 s[{s.s_tmp(5)}], {thread_stride}")
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080000 ; offset:0, width:8")
                self._emit(m_mdiv_u32_ss(s.s_tmp(4), s.s_thread_stride_n(), s.s_tmp(5), s.s_magic_4(), s.s_tmp(3), s.s_dim_b(), s.s_tmp()))
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080008 ; offset:8, width:8")
                self._emit(m_mdiv_u32_ss(s.s_thread_stride_w(), s.s_thread_stride_h(), s.s_tmp(4), s.s_magic_5(), s.s_tmp(3), s.s_wo(), s.s_tmp()))
            else:
                self._emit(m_int_div_rem_ss(s.s_tmp(4), s.s_thread_stride_n(), s.s_tmp(5), s.s_dim_b(), v.v_tmp(5), v.v_tmp(), s.s_tmp()))
                self._emit(m_int_div_rem_ss(s.s_thread_stride_w(), s.s_thread_stride_h(), s.s_tmp(4), s.s_wo(), v.v_tmp(5), v.v_tmp(), s.s_tmp()))

            if self.tunable.nxe != 0:
                self._emit(f"s_mul_i32 s[{s.s_thread_stride_h()}], s[{s.s_thread_stride_h()}], s[{s.s_stride_h()}]")
                self._emit(f"s_mul_i32 s[{s.s_thread_stride_w()}], s[{s.s_thread_stride_w()}], s[{s.s_stride_w()}]")

            # now let's precompute all the voffset
            # ihi = iho * s_stride_h + iy * s_dilation_h - s_pad_h
            # iwi = iwo * s_stride_w + ix * s_dilation_w - s_pad_w
            self._emit(f"v_mov_b32 v[{v.v_tmp(5)}], v[{v.v_in_ihi()}]")
            self._emit(f"v_mov_b32 v[{v.v_tmp(3)}], v[{v.v_in_in()}]")
            nb_per_thread = ta_nb0 if ta_nb0 != 1 else ta_nb1
            for i in range(1, nb_per_thread):
                # v_tmp+4:ihi, v_tmp+5:iwi
                self._emit(f"v_add_i32 v[{v.v_tmp(4)}], s[{s.s_thread_stride_w()}], v[{v.v_in_iwi() if i == 1 else v.v_tmp(4) }]")
                self._emit(f"v_cmpx_le_i32 vcc, s[{s.s_lim_w()}], v[{v.v_tmp(4)}]")
                self._emit(f"v_subrev_i32 v[{v.v_tmp(4)}], s[{s.s_len_w()}], v[{v.v_tmp(4)}]")
                if self.tunable.nxe != 0:
                    self._emit(f"v_add_i32 v[{v.v_tmp(5)}],  s[{s.s_stride_h()}], v[{v.v_tmp(5)}]")
                else:
                    self._emit(f"v_add_i32 v[{v.v_tmp(5)}],  1, v[{v.v_tmp(5)}]")
                self._emit(f"s_mov_b64 exec, -1")

                self._emit(f"v_add_i32 v[{v.v_tmp(5)}], s[{s.s_thread_stride_h()}], v[{v.v_tmp(5)}]")
                self._emit(f"v_cmpx_le_i32 vcc, s[{s.s_lim_h()}], v[{v.v_tmp(5)}]")
                self._emit(f"v_subrev_i32 v[{v.v_tmp(5)}], s[{s.s_len_h()}], v[{v.v_tmp(5)}]")
                self._emit(f"v_add_u32 v[{v.v_tmp(3)}], 1, v[{v..v_tmp(3)}]")
                self._emit(f"s_mov_b64 exec, -1")

                self._emit(f"v_add_u32 v[{v.v_tmp(3)}], s[{s.s_thread_stride_n()}], v[{v.v_tmp(3)}]")

                if self.tunable.nxe != 0:
                    # update flag for batch size
                    self._emit(f"v_cmp_gt_u32 vcc, s[{s.s_n()}], v[{v.v_tmp(3)}]")
                    self._emit(f"v_cndmask_b32 v[{v.v_tmp()}], 0, 1, vcc")
                    self._emit(m_set_flag_nhw(v.v_flag(i), v.v_tmp(), v.v_tmp(5), v.v_tmp(4), s.s_hi(), s.s_wi()))

                self._emit(f"v_mul_lo_u32 v[{v.v_tmp(1)}], s[{s.s_in_stride_n()}], v[{v.v.v_tmp(3)}]")
                self._emit(f"v_add_lshl_u32 v[{v.v_tmp(2)}], v[{v.v_gtc_ic()}], v[{v.v_tmp(1)}], {igemm_log2(data_byte)}")
                self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_wi()}], v[{v.v_tmp(5)}]")
                self._emit(f"v_add_u32 v[{v.v_tmp()}], v[{v.v_tmp(4)}], v[{v.v_tmp()}]")
                self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_in_stride_wi()}], v[{v.v_tmp()}]")
                self._emit(f"v_add_u32 v[{v.v_in_os(i)}], v[{v.v_tmp(2)}], v[{v.v_tmp()}]")

        else:
            pass

        # load in
        self._emit(self.global_load_in())
        self._emit_empty_line()

        self._emit(f"s_mov_b32 s[{s.s_p_wei(2)}], 0xffffffff")
	    # config weight range
        #self._emit("; config for weight range")
        #self._emit(f"s_mul_i32 s[{s.s_p_wei(2)}], s[{s.s_wei_stride_k() if self.tunable.nxe != 0 else s.s_c()}], s[{s.s_k()}]")
        #self._emit(f"s_lshl_b32 s[{s.s_p_wei(2)}], s[{s.s_p_wei(2)}], {igemm_log2(data_byte)}")
        self._emit(f"s_mov_b32 s[{s.s_p_wei(3)}], 0x27000")

        self._emit(f"; calculate wei offset")
        self._emit(f"s_mul_i32 s[{s.s_tmp(2)}], s[{s.s_k()}], s[{s.s_wei_stride_k()}]")
        self._emit(f"s_mul_i32 s[{s.s_tmp()}], s[{s.s_block_gtc_ig()}], s[{s.s_tmp(2)}]")
        self._emit(f"s_mul_hi_u32 s[{s.s_tmp(1)}], s[{s.s_block_gtc_ig()}], s[{s.s_tmp(2)}]")
        self._emit(f"s_add_u32 s[{s.s_p_wei()}], s[{s.s_p_wei()}], s[{s.s_tmp()}]")
        self._emit(f"s_addc_u32 s[{s.s_p_wei(1)}], s[{s.s_p_wei(1)}], s[{s.s_tmp(1)}]") 

        self._emit(f"v_add_u32 v[{v.v_cur_k()}], s[{s.s_block_gtc_ik()}], v[{v.v_wei_ik()}]")
        self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_wei_stride_k()}], v[{v.v_cur_k()}]")
        self._emit(f"v_add_lshl_u32 v[{v.v_wei_os()}], v[{v.v_tmp()}], v[{v.v_gtc_ic()}], {igemm_log2(data_byte)}")

        self._emit_empty_line()
        if self.wei_thread_copy_ndim != 1:
            if s_wei_stride_d0 != s_dummy:
                self._emit(self.try_shift_stride(s_wei_stride_d0, igemm_log2(data_byte)))
        if s_wei_stride_d1 != s_dummy:
            self._emit(self.try_shift_stride(s_wei_stride_d1, igemm_log2(data_byte)))
        self._emit_empty_line()

        if self.tunable.precache_soffset:
            self._emit(m_wei_2d_global_load.init_precache_soffset(s_wei_stride_d0(), s_wei_stride_d1(), s.s_wei_offset(), s.s_tmp()))

        self._emit(self.global_load_wei())
        self._emit_empty_line()

        if self.tunable.fma_type != IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS:
            self._emit(f"v_mov_b32 v[{v.v_tmp(5)}], v0")
            self._emit(self.thread_mapping(v.v_gemm_in(), v.v_gemm_im(), v.v_tmp(5), v.v_tmp()))
        else:
            self._emit(f"v_mov_b32 v[{v.v_tmp(5)}], v0")
            self._emit(self.xdlops_mapping.get_gemm_index_for_src_matrix(v.v_gemm_in(), v.v_gemm_im(), v.v_tmp(5), v.v_tmp()))
            self._emit(f"v_mov_b32 v[{v.v_tmp(5)}], v0")
            self._emit(self.xdlops_mapping.get_gemm_index_for_dst_matrix(v.v_co_sst(), v.v_co_sld(), v.v_tmp(5), v.v_tmp()))

        '''
        gemm_k * gemm_m * k_pack
        '''
        self._emit(f"; LDS store, in: e,c,nb0,nb1: {ta_e}x{ta_c}x{ta_nb0}x{ta_nb1}, {ca_e}x{ca_c}x{ca_nb0}x{ca_nb1}")
        if ca_nb1 == 1:
            # TODO: remove this path, not possible go here
            assert False
        else:
            if ca_nb0 == 1:
                self._emit(f"v_mov_b32 v[{v.v_tmp()}], v[{v.v_in_inb()}]")
            else:
                self._emit(f"v_lshl_or_b32 v[{v.v_tmp()}], v[{v.v_gtc_ta_in0()}], {igemm_log2(na_nb1)}, v[{v.v_in_inb()}]")
        self._emit(f"v_lshl_or_b32 v[{v.v_tmp()}], v[{v.v_gtc_ic()}], {igemm_log2(na_nb0*na_nb1)}, v[{v.v_tmp()}]")
        self._emit(f"v_lshlrev_b32 v[{v.v_sst_a_os()}], {igemm_log2(data_byte)}, v[{v.v_tmp()}]")
        self._emit_empty_line()

        self._emit(f"; LDS store, wei: e,c,k: {ta_e}x{ta_c}x{tb_k}, {ca_e}x{ca_c}x{cb_k}")
        self._emit(f"v_lshl_or_b32 v[{v.v_tmp()}], v[{v.v_gtc_ic()}], {igemm_log2(nb_k)}, v[{v.v_wei_ik()}]")
        self._emit(f"v_lshlrev_b32 v[{v.v_sst_b_os()}], {igemm_log2(data_byte)}, v[{v.v_tmp()}]")
        self._emit(f"v_add_u32 v[{v.v_sst_b_os()}], {self.tunable.lds_a_np2}, v[{v.v_sst_b_os()}]")
        self._emit_empty_line()

        self._emit(f"; LDS load")
        self._emit(f"v_lshlrev_b32 v[{v.v_sld_b_os()}], {igemm_log2(data_byte)}, v[{v.v_gemm_in()}]")
        self._emit(f"v_lshlrev_b32 v[{v.v_sld_a_os()}], {igemm_log2(data_byte)}, v[{v.v_gemm_im()}]")
        self._emit(f"v_add_u32 v[{v.v_sld_b_os()}], {self.tunable.lds_a_np2}, v[{v.v_sld_b_os()}]")
        self._emit_empty_line()

        if self.tunable.fma_type == IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS:
            self._emit(f"v_mov_b32 v[{v.v_gemm_in()}], v[{v.v_co_sst()}]")
            self._emit(f"v_mov_b32 v[{v.v_gemm_im()}], v[{v.v_co_sld()}]")
        self._emit(self.coalescing_store.init_co_lds_offset(v.v_co_sst(), v.v_co_sld(), v.v_gemm_im(), v.v_gemm_in(), '0', v.v_tmp()))
        self._emit(self.coalescing_store.init_co_sub_m_index(v.v_co_sub_m_index(), '0', v.v_tmp()))
        self._emit(self.coalescing_store.init_co_sub_n_index(v.v_co_sub_n_index(), '0', v.v_tmp()))
        self._emit_empty_line()

        '''
        a good news for nhwc and coalescing output is that, we can treat gemm_m (n*ho*wo) as a single dimension,
        and use sgpr to stride along this dimension. this is much easier
        '''
        self._emit(f"; output offset")
        self._emit(f"s_mul_i32 s[{s.s_tmp()}], s[{s.s_block_gtc_ig()}], s[{s.s_k()}]")
        self._emit(f"s_mul_hi_u32 s[{s.s_tmp(1)}], s[{s.s_block_gtc_ig()}], s[{s.s_k()}]")
        self._emit(f"s_add_u32 s[{s.s_p_out()}], s[{s.s_p_out()}], s[{s.s_tmp()}]")
        self._emit(f"s_addc_u32 s[{s.s_p_out(1)}], s[{s.s_p_out(1)}], s[{s.s_tmp(1)}]")

        self._emit(f"s_lshl_b32 s[{s.s_tmp(3)}], s[{s.s_block_gtc_in0()}], {igemm_log2(unmerge_sub_n1 * data_byte)}")
        self._emit(f"s_mul_i32 s[{s.s_tmp()}], s[{s.s_out_stride_n()}], s[{s.s_tmp(3)}]")
        self._emit(f"s_mul_hi_u32 s[{s.s_tmp(1)}], s[{s.s_out_stride_n()}], s[{s.s_tmp(3)}]")
        self._emit(f"s_add_u32 s[{s.s_p_out()}], s[{s.s_p_out()}], s[{s.s_tmp()}]")
        self._emit(f"s_addc_u32 s[{s.s_p_out(1)}], s[{s.s_p_out(1)}], s[{s.s_tmp(1)}]")

        self._emit_empty_line()
        self._emit(f"s_lshl_b32 s[{s.s_tmp(3)}], s[{s.s_block_gtc_ik()}], {igemm_log2(data_byte)}")

        self._emit(f"s_add_u32 s[{s.s_p_out()}], s[{s.s_p_out()}], s[{s.s_tmp(3)}]")
        self._emit(f"s_addc_u32 s[{s.s_p_out(1)}], s[{s.s_p_out()}+1], 0")
        self._emit_empty_line()

        self._emit(f"; compute v_co_sub_m_index along nb0 x nb1 : {na_nb0}x{na_nb1}")
        if gemm_m_order == IGEMM_FWD_GTC_NHWC_LDS_STORE_ORDER_GEMM_M_N0_N1B:
            if na_nb1 != 1:
                self._emit(f"v_and_b32 v[{v.v_out_in1b()}], {na_nb1 - 1}, v[{v.v_co_sub_m_index()}]     ; => N1B")
                if na_nb0 != 1:
                    self._emit(f"v_lshrrev_b32 v[{v.v_out_in0()}], {igemm_log2(na_nb1)}, v[{v.v_co_sub_m_index()}]  ; => N0")
            else:
                assert False, "un implemented, should rarely be used"
        else:
            assert False

        # TODO: extend tensor size, here vgpr only have 32bit
        self._emit(f";   compute from nb1")
        self._emit(f"v_add_u32 v[{v.v_tmp(5)}], s[{s.s_block_gtc_in1b()}], v[{v.v_out_in1b()}]")
        if self.tunable.nxe != 0:
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080000 ; offset:0, width:8")
                self._emit(m_mdiv_u32_vs(v.v_tmp(4), v.v_out_in1(), v.v_tmp(5), s.s_magic_4(), s.s_tmp(3), s.s_dim_b(), v.v_tmp()))
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080008 ; offset:8, width:8")
                self._emit(m_mdiv_u32_vs(v.v_out_iwo(), v.v_out_iho(), v.v_tmp(4), s.s_magic_5(), s.s_tmp(3), s.s_wo(), v.v_tmp()))
            else:
                self._emit(m_int_div_rem_vs(v.v_tmp(4), v.v_out_in1(), v.v_tmp(5), s.s_dim_b(), v.v_tmp(), s.s_tmp()))
                self._emit(m_int_div_rem_vs(v.v_out_iwo(), v.v_out_iho(), v.v_tmp(4), s.s_wo(), v.v_tmp(), s.s_tmp()))
            self._emit_empty_line()
        else:
            if IGEMM_GTC_FEAT_MAGIC_DIVISION:
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080000 ; offset:0, width:8")
                self._emit(m_mdiv_u32_vs(v.v_tmp(4), v.v_out_in1(), v.v_tmp(5), s.s_magic_4(), s.s_tmp(3), s.s_dim_b(), v.v_tmp()))
                self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_1()}], 0x00080008 ; offset:8, width:8")
                self._emit(m_mdiv_u32_vs(v.v_out_iwo(), v.v_out_iho(), v.v_tmp(4), s.s_magic_5(), s.s_tmp(3), s.s_wi(), v.v_tmp()))
            else:
                self._emit(m_int_div_rem_vs(v.v_tmp(4), v.v_out_in1(), v.v_tmp(5), s.s_dim_b(), v.v_tmp(), s.s_tmp()))
                self._emit(m_int_div_rem_vs(v.v_out_iwo(), v.v_out_iho(), v.v_tmp(4), s.s_wi(), v.v_tmp(), s.s_tmp()))
            self._emit_empty_line()
        self._emit_empty_line()
        self._emit(f"; add in_in0, in_in1")
        if na_nb0 != 1:
            #if gemm_m_unmerge_cluster == 0:
            self._emit(f"v_lshl_or_b32 v[{v.v_tmp(1)}], v[{v.v_out_in0()}], {igemm_log2(unmerge_sub_n1)}, v[{v.v_out_in1()}]")
            self._emit(f"v_mul_lo_u32 v[{v.v_out_os()}], s[{s.s_out_stride_n()}], v[{v.v_tmp(1)}]")
            # else:
            #     self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_out_stride_n()}], v[{v.v_out_in1()}]")
            #     self._emit(f"v_mul_lo_u32 v[{v.v_tmp(1)}], s[{s.s_out_stride_n0()}], v[{v.v_out_in0()}]")
            #     self._emit(f"v_add_u32 v[{v.v_out_os()}], v[{v.v_tmp()}], v[{v.v_tmp(1)}]")
        else:
            self._emit(f"v_mul_lo_u32 v[{v.v_out_os()}], s[{s.s_out_stride_n()}], v[{v.v_out_in1()}]")

        self._emit(f"; add i_k")
        ## gemm_m_unmerge_cluster is always 0
        # if gemm_m_order == IGEMM_FWD_GTC_LDS_STORE_ORDER_GEMM_M_K0_K1:
        #     self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_out_stride_k()}], v[{v.v_co_sub_m_index()}]")
        # else:
        #     if na_k0 == 1:
        #         self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_out_stride_k()}], v[{v.v_co_sub_m_index()}]")
        #     else:
        #         if na_k1 == 1:
        #             self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_out_stride_k()}], v[{v.v_co_sub_m_index()}]")
        #         else:
        #             self._emit(f"v_and_b32 v[{v.v_tmp()}], {na_k0 - 1}, v[{v.v_co_sub_m_index()}]        ; => k0")
        #             self._emit(f"v_lshrrev_b32 v[{v.v_tmp(1)}], {igemm_log2(na_k0)}, v[{v.v_co_sub_m_index()}]       ; => k1")
        #             self._emit(f"v_lshl_or_b32 v[{v.v_tmp(1)}], v[{v.v_tmp()}], {igemm_log2(na_k1)}, v[{v.v_tmp(1)}]")
        #             self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_out_stride_k()}], v[{v.v_tmp(1)}]")

        self._emit(f"v_add_u32 v[{v.v_out_os()}], v[{v.v_out_os()}], v[{v.v_co_sub_n_index()}]")    # n, add to k

        self._emit(f"; add ho, wo")
        self._emit(f"s_mul_i32 s[{s.s_tmp()}], s[{s.s_k()}], s[{s.s_group()}]   ; stride for wo")
        self._emit(f"v_mul_lo_u32 v[{v.v_tmp(1)}], s[{s.s_wo() if self.tunable.nxe != 0 else s.s_wi()}], v[{v.v_out_iho()}]")
        self._emit(f"v_add_u32 v[{v.v_tmp(2)}], v[{v.v_tmp(1)}], v[{v.v_out_iwo()}]")
        self._emit(f"v_mul_lo_u32 v[{v.v_tmp()}], s[{s.s_tmp()}], v[{v.v_tmp(2)}]")
        self._emit(f"v_add_u32 v[{v.v_out_os()}], v[{v.v_out_os()}], v[{v.v_tmp()}]")
        self._emit(f"v_lshlrev_b32 v[{v.v_out_os()}], {igemm_log2(data_byte)}, v[{v.v_out_os()}]")
        if self.tunable.nxe != 0:
            self._emit(m_set_flag_hw(v.v_out_flag(), v.v_out_iho(), v.v_out_iwo(), s.s_ho(), s.s_wo()))

        self._emit(f"; move slice stride")
        self._emit(f"s_mov_b32 s[{s.s_gemm_k_num_c()}], s[{s.s_c()}]")
        if self.tunable.nxe != 0:
            self._emit(f"s_mov_b32 s[{s.s_move_slice_k_c()}], {na_c}")
            self._emit(f"s_mul_i32 s[{s.s_move_slice_k_stride_c()}], s[{s.s_move_slice_k_c()}], {igemm_log2(data_byte)}")
        else:
            self._emit(f"s_mov_b32 s[{s.s_move_slice_k_stride_c()}], {na_c * data_byte}")

        if self.tunable.nxe != 0:
            self._emit(f"s_lshl_b32 s[{s.s_tmp(2)}], s[{s.s_c()}], {igemm_log2(data_byte)}")
            # diff_y, ihi = iho * s_stride_h + iy * s_dilation_h - s_pad_h
            self._emit(f"s_mul_i32 s[{s.s_tmp(4)}], s[{s.s_wi()}], s[{s.s_tmp(4)}]")
            self._emit(f"s_mul_i32 s[{s.s_move_slice_k_in_stride_diff_y()}], s[{s.s_dilation_h()}], s[{s.s_tmp(4)}]")
            self._emit(self.try_shift_stride(s.s_move_slice_k_in_stride_diff_y, igemm_log2(data_byte)))
            self._emit(f"s_sub_u32 s[{s.s_move_slice_k_in_stride_diff_y()}], s[{s.s_move_slice_k_in_stride_diff_y()}], s[{s.s_tmp(2)}]")
            # diff_x, iwi = iwo * s_stride_w + ix * s_dilation_w - s_pad_w, hence need compute s_dilation_w per increase
            self._emit(f"s_mul_i32 s[{s.s_tmp(4)}], s[{s.s_c()}], s[{s.s_group()}]")
            self._emit(f"s_mul_i32 s[{s.s_move_slice_k_in_stride_diff_x()}], s[{s.s_dilation_w()}], s[{s.s_tmp(4)}]")
            self._emit(self.try_shift_stride(s.s_move_slice_k_in_stride_diff_x, igemm_log2(data_byte)))
            self._emit(f"s_sub_u32 s[{s.s_move_slice_k_in_stride_diff_x()}], s[{s.s_move_slice_k_in_stride_diff_x()}], s[{s.s_tmp(2)}]")

            self._emit(f"s_mul_i32 s[{s.s_in_diff_sub_wi()}], s[{s.s_x()}], s[{s.s_dilation_w()}] ")

        # assert na_c0 * na_c1e == self.tunable.gemm_k_per_block and nb_c0 * nb_c1e == self.tunable.gemm_k_per_block

        # if self.tunable.nxe != 0:
        #     #assert na_c0 * na_c1e == nb_c0 * nb_c1e
        #     self._emit(f"s_mov_b32 s[{s.s_move_slice_k_c1e()}], {na_c0 * na_c1e}")
        #     if IGEMM_GTC_FEAT_MAGIC_DIVISION:
        #         self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_0()}], 0x00080010 ; offset:16, width:8")
        #         self._emit(m_mdiv_u32_ss(s.s_tmp(4), s.s_move_slice_k_c1(), s.s_move_slice_k_c1e(), s.s_magic_2(), s.s_tmp(3), s.s_stride_c(), s.s_tmp()))
        #         self._emit(f"s_bfe_u32 s[{s.s_tmp(3)}], s[{s.s_shift_pack_0()}], 0x00080018 ; offset:24, width:8")
        #         self._emit(m_mdiv_u32_ss(s.s_move_slice_k_x(), s.s_move_slice_k_y(), s.s_tmp(4), s.s_magic_3(), s.s_tmp(3), s.s_x(), s.s_tmp()))
        #     else:
        #         self._emit(m_int_div_rem_ss(s.s_tmp(4), s.s_move_slice_k_c1(), s.s_move_slice_k_c1e(), s.s_stride_c(), v.v_tmp(4), v.v_tmp(), s.s_tmp()))
        #         self._emit(m_int_div_rem_ss(s.s_move_slice_k_x(), s.s_move_slice_k_y(), s.s_tmp(4), s.s_x(), v.v_tmp(4), v.v_tmp(), s.s_tmp()))
        # else:
        #     #assert na_c1e == nb_c1e
        #     #self._emit(f"s_mov_b32 s[{s.s_move_slice_k_c1()}], {nb_c1e}")
        #     self._emit(f"s_mov_b32 s[{s.s_move_slice_k_c1e()}], {na_c1e}")
        # self._emit_empty_line()

        # m_move_slice_window_ta, m_move_slice_window_tb = self.get_macro_move_slice_window()

        # if self.tunable.nxe != 0:
        #     # assert s.s_out_stride_k.label not in self.dict_shifted_stride and s.s_wei_stride_k.label not in self.dict_shifted_stride
        #     if s.s_stride_c.label not in self.dict_shifted_stride:
        #         self._emit(m_move_slice_window_tb.init_stride_c(s.s_stride_c(), s.s_in_stride_c_c1(),
        #                                                 s.s_in_stride_c_c0_c1_diff(), s.s_move_slice_k_c1()))
        #     else:
        #         self._emit(f"s_lshr_b32 s[{s.s_tmp(3)}], s[{s.s_stride_c()}], {utility_log2(data_byte)}")
        #         self._emit(m_move_slice_window_tb.init_stride_c(s.s_tmp(3), s.s_in_stride_c_c1(),
        #                                                 s.s_in_stride_c_c0_c1_diff(), s.s_move_slice_k_c1()))
        # else:
        #     if self.is_1d_move_slice_k():
        #         self._emit(m_move_slice_window_tb.init_stride_c(s.s_stride_hw(), s.s_in_stride_c_c1(),  s.s_move_slice_k_c1e()))
        #     else:
        #         self._emit(m_move_slice_window_tb.init_stride_c(s.s_stride_hw(), s.s_in_stride_c_c1(), 
        #                                                 s.s_in_stride_c_c0_c1_diff(), s.s_move_slice_k_c1e()))


        # if not self.is_1d_move_slice_k():
        #     self._emit(f"s_mov_b32 s[{s.s_gemm_k_num_c1()}], {unmerge_sub_tb_c1}")
        #if self.tunable.nxe != 0:
        #    self._emit(f"s_mul_i32 s[{s.s_knum()}], s[{s.s_stride_c()}], s[{s.s_c()}]")
        #else:
        #    self._emit(f"s_mov_b32 s[{s.s_knum()}], s[{s.s_c()}]")
        self._emit_empty_line()

        #self._emit(self.try_shift_stride(s.s_in_stride_c_c1, igemm_log2(data_byte)))
        #self._emit(self.try_shift_stride(s.s_wei_stride_k_k1, igemm_log2(data_byte)))
        #self._emit(self.try_shift_stride(s.s_in_stride_c_c0_c1_diff, igemm_log2(data_byte)))
        #self._emit(self.try_shift_stride(s.s_wei_stride_k_k0_k1_diff, igemm_log2(data_byte)))

        if self.tunable.nxe != 0:
            # self._emit(self.try_shift_stride(s.s_stride_c, igemm_log2(data_byte)))
            self._emit(self.try_shift_stride(s.s_wei_stride_k, igemm_log2(data_byte)))
            # self._emit(self.try_shift_stride(s.s_out_stride_k, igemm_log2(data_byte)))
        else:
            # self._emit(self.try_shift_stride(s.s_stride_c, igemm_log2(data_byte)))
            self._emit(self.try_shift_stride(s.s_c, igemm_log2(data_byte)))
            # self._emit(self.try_shift_stride(s.s_out_stride_k, igemm_log2(data_byte)))

        # self._emit(self.try_shift_stride(s.s_move_slice_k_c1e, igemm_log2(data_byte)))
        self._emit(f"s_mov_b32 s[{s.s_p_out(2)}], 0xffffffff")
        self._emit(f"s_mov_b32 s[{s.s_p_out(3)}], 0x27000")


    def emit_kernel_fma_main_loop(self):
        s = self.sgpr
        v = self.vgpr
        data_byte = amdgpu_precision_data_byte(self.tunable.precision)

        # m_move_slice_window_ta, m_move_slice_window_tb = self.get_macro_move_slice_window()
        m_move_slice_window = self.get_macro_move_slice_window()
        m_set_flag_hw       = self.get_macro_set_flag_hw()

        def move_slice_window_b():
            '''
            in nhwc we only need call one move slice window
            '''
            if self.tunable.nxe != 0:
                with self._deferred_context():
                    self._emit(m_move_slice_window(v.v_move_slice_k_iy(), v.v_move_slice_k_ix(), v.v_move_slice_k_ic(),
                                s.s_gemm_k_num_x(), s.s_gemm_k_num_c(), s.s_move_slice_k_c(), v.v_in_os(), v.v_wei_os(),
                                s.s_move_slice_k_in_stride_diff_y(), s.s_move_slice_k_in_stride_diff_x(), s.s_move_slice_k_stride_c(),
                                v.v_in_ihi(), v.v_in_iwi(), s.s_dilation_h(), s.s_dilation_w(), s.s_in_diff_sub_wi()))
                    self._emit(m_set_flag_hw(v.v_in_flag(), v.v_in_ihi(), v.v_in_iwi(), s.s_hi(), s.s_wi()))
                return self._get_deferred()
            else:
                with self._deferred_context():
                    self._emit(m_move_slice_window(v.v_in_os(), v.v_wei_os(),s.s_move_slice_k_stride_c()))
                return self._get_deferred()

        def move_slice_window_a():
            return ''

        if self.tunable.fma_type != IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS:
            fctrl                             = ctrl_fma_main_loop_t()
            fctrl.thread_m                    = self.tunable.thread_tile_m
            fctrl.thread_n                    = self.tunable.thread_tile_n
            fctrl.unroll_k                    = self.tunable.gemm_k_per_block
            fctrl.label_prefix                = self.name()
            fctrl.gemm_m_repeat               = self.tunable.gemm_m_repeat
            fctrl.gemm_m_level0_cluster       = self.tunable.gemm_m_level0_cluster
            fctrl.gemm_m_level1_cluster       = self.tunable.gemm_m_level1_cluster
            fctrl.gemm_n_repeat               = self.tunable.gemm_n_repeat
            fctrl.gemm_n_level0_cluster       = self.tunable.gemm_n_level0_cluster
            fctrl.gemm_n_level1_cluster       = self.tunable.gemm_n_level1_cluster
            fctrl.lds_single_size             = self.tunable.lds_single            # in byte, should be power of 2
            fctrl.lds_buffer_num              = self.tunable.lds_buffer_num

            # functor
            fctrl.global_load_a_functor       = self.global_load_wei
            fctrl.global_load_b_functor       = self.global_load_in
            fctrl.shared_store_a_functor      = self.shared_store_wei
            fctrl.shared_store_b_functor      = self.shared_store_in
            fctrl.shared_load_a_functor       = inst_ds_read_t(self.tunable.thread_sub_tile_m * data_byte)
            fctrl.shared_load_b_functor       = inst_ds_read_t(self.tunable.thread_sub_tile_n * data_byte)
            fctrl.move_slice_window_a_functor = move_slice_window_a
            fctrl.move_slice_window_b_functor = move_slice_window_b

            # sympol type
            fctrl.v_a                         = v.v_a
            fctrl.v_b                         = v.v_b
            fctrl.v_c                         = v.v_c
            fctrl.v_gld_a                     = v.v_gld_a
            fctrl.v_gld_b                     = v.v_gld_b
            fctrl.v_sld_a_os                  = v.v_sld_a_os
            fctrl.v_sld_b_os                  = v.v_sld_b_os
            fctrl.v_sst_a_os                  = v.v_sst_a_os
            fctrl.v_sst_b_os                  = v.v_sst_b_os
            fctrl.s_kitr                      = s.s_kitr
            fctrl.s_knum                      = s.s_knum

            fma_main_loop = fma_main_loop_t(self.mc, fctrl)
            fma_main_loop.emit()
        else:
            a = self.agpr
            fctrl                             = ctrl_mfma_main_loop_t()
            ctrl_xdlops_mapping               = get_ctrl_xdlops_mapping_from_wave_tile_fp32(self.tunable.gemm_m_per_block, self.tunable.gemm_n_per_block,
                                                                        self.tunable.wave_tile_m, self.tunable.wave_tile_n, self.tunable.wave_tile_k,
                                                                        self.tunable.wave_repeat_m, self.tunable.wave_repeat_n,
                                                                        self.tunable.wave_step_m, self.tunable.wave_step_n, self.tunable.block_size // AMDGPU_WAVE_SIZE)
            fctrl.cxm                         = ctrl_xdlops_mapping
            fctrl.unroll_k                    = self.tunable.gemm_k_per_block
            fctrl.label_prefix                = self.name()
            fctrl.lds_single_size             = self.tunable.lds_single            # in byte, should be power of 2
            fctrl.lds_buffer_num              = self.tunable.lds_buffer_num
            fctrl.local_prefetch_num          = self.tunable.local_prefetch_num
            fctrl.interleave                  = self.tunable.fma_interleave

            # functor
            fctrl.global_load_a_functor       = self.global_load_wei
            fctrl.global_load_b_functor       = self.global_load_in
            fctrl.shared_store_a_functor      = self.shared_store_wei
            fctrl.shared_store_b_functor      = self.shared_store_in
            if ctrl_xdlops_mapping.wave_step_m == 1:
                fctrl.shared_load_a_functor   = inst_ds_read_t(data_byte)   # xdlops load from LDS always single load
            else:
                assert ctrl_xdlops_mapping.wave_step_m == 2, "currently only support wave_step_m is 2"
                fctrl.shared_load_a_functor   = inst_ds_read2_likely_accumulate_offset_t(self.mc, 2, data_byte, ctrl_xdlops_mapping.wave_tile_m * data_byte, sym_t(self.vgpr.v_tmp(4)))

            if ctrl_xdlops_mapping.wave_step_n == 1:
                fctrl.shared_load_b_functor   = inst_ds_read_t(data_byte)   # xdlops load from LDS always single load
            else:
                assert ctrl_xdlops_mapping.wave_step_n == 2, "currently only support wave_step_n is 2"
                fctrl.shared_load_b_functor   = inst_ds_read2_likely_accumulate_offset_t(self.mc, 2, data_byte, ctrl_xdlops_mapping.wave_tile_n * data_byte, sym_t(self.vgpr.v_tmp(5)))
            fctrl.move_slice_window_a_functor = move_slice_window_a
            fctrl.move_slice_window_b_functor = move_slice_window_b

            # sympol type
            fctrl.v_a                         = v.v_a
            fctrl.v_b                         = v.v_b
            fctrl.a_c                         = a.a_c
            fctrl.v_gld_a                     = v.v_gld_a
            fctrl.v_gld_b                     = v.v_gld_b
            fctrl.v_sld_a_os                  = v.v_sld_a_os
            fctrl.v_sld_b_os                  = v.v_sld_b_os
            fctrl.v_sst_a_os                  = v.v_sst_a_os
            fctrl.v_sst_b_os                  = v.v_sst_b_os
            fctrl.s_kitr                      = s.s_kitr
            fctrl.s_knum                      = s.s_knum

            mfma_main_loop = mfma_main_loop_t(self.mc, fctrl)
            mfma_main_loop.emit()


    def emit_kernel_epilogue(self):
        s = self.sgpr
        v = self.vgpr
        #label_out = f"L_{self.name()}_out"

        ta_nb0, ta_nb1, ta_e, ta_c, tb_k0, tb_k1 = self.get_thread_lengths()
        ca_nb0, ca_nb1, ca_e, ca_c, cb_k0, cb_k1 = self.get_cluster_lengths()

        if self.tunable.fma_type != IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS:
            # if self.tunable.nxe != 0:
            #     self._emit(self.coalescing_store(v.v_c(), v.v_co_sst(), v.v_co_sld(), s.s_p_in(), v.v_in_os(), None,
            #         s.s_in_stride_c0() if self.tunable.gemm_m_unmerge_cluster == 1 else None, s.s_stride_c(), s.s_tmp(), v.v_in_flag()))
            # else:
            #     self._emit(self.coalescing_store(v.v_c(), v.v_co_sst(), v.v_co_sld(), s.s_p_in(), v.v_in_os(), None,
            #         s.s_in_stride_c0() if self.tunable.gemm_m_unmerge_cluster == 1 else None, s.s_stride_c(), s.s_tmp()))
            assert False
        else:
            a = self.agpr
            self._emit(self.coalescing_store(a.a_c(), v.v_c(), v.v_co_sst(), v.v_co_sld(), s.s_p_out(), v.v_out_os(), None,
                     s.s_out_stride_n0() if ta_nb0 != 1 else None, s.s_out_stride_wo(),
                     s.s_tmp(), v.v_out_flag() if self.tunable.nxe != 0 else None, s.s_k(), v.v_cur_k(), s.s_block_gtc_ik(), v.v_co_sub_m_index(), v.v_tmp()))

        self._emit_front(f"{self.label_out}:")

    def emit_kernel_symbol(self):
        self.karg.emit()
        self._emit_empty_line()
        self.sgpr.emit()
        self._emit_empty_line()
        self.vgpr.emit()
        self._emit_empty_line()
        if self.tunable.fma_type == IGEMM_GTC_TUNABLE_FMA_TYPE_XDLOPS:
            self.agpr.emit()
            self._emit_empty_line()

    def emit_kernel_header(self):
        kernel_name = self.name()
        self._emit('.text')
        if self.mc.arch_config.code_object == AMDGPU_CODEOBJECT_V3:
            self._emit('.globl {}'.format(kernel_name))
        self._emit('.p2align 8')
        if self.mc.arch_config.code_object == AMDGPU_CODEOBJECT_V3:
            self._emit('.type {},@function'.format(kernel_name))
        if self.mc.arch_config.code_object == AMDGPU_CODEOBJECT_V2:
            self._emit('.amdgpu_hsa_kernel {}'.format(kernel_name))
        self._emit('{}:'.format(kernel_name))

    def emit_kernel_body(self):
        self.emit_kernel_prologue()
        self.emit_kernel_fma_main_loop()
        self.emit_kernel_epilogue()
    def emit_kernel_end(self):
        self._emit('s_endpgm')
    def emit_kernel_footer(self):
        self._emit_empty_line()

    def emit_kernel_amd_kernel_code_t(self):
        amd_kernel_code_t(self.mc, self.get_kernel_info()).emit()
