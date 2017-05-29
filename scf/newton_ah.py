#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''
Co-iterative augmented hessian (CIAH) second order SCF solver
'''

import sys
import time
import copy
from functools import reduce
import numpy
import scipy.linalg
from pyscf import lib
from pyscf import gto
from pyscf import symm
from pyscf.lib import logger
from pyscf.scf import chkfile
from pyscf.scf import addons

# http://scicomp.stackexchange.com/questions/1234/matrix-exponential-of-a-skew-hermitian-matrix-with-fortran-95-and-lapack
def expmat(a):
    return scipy.linalg.expm(a)

def gen_g_hop_rhf(mf, mo_coeff, mo_occ, fock_ao=None, h1e=None):
    mol = mf.mol
    occidx = numpy.where(mo_occ==2)[0]
    viridx = numpy.where(mo_occ==0)[0]
    nocc = len(occidx)
    nvir = len(viridx)

    if fock_ao is None:
        if h1e is None: h1e = mf.get_hcore()
        dm0 = mf.make_rdm1(mo_coeff, mo_occ)
        fock_ao = h1e + mf.get_veff(mol, dm0)
    fock = reduce(numpy.dot, (mo_coeff.T, fock_ao, mo_coeff))

    g = fock[occidx[:,None],viridx].T * 2

    foo = fock[occidx[:,None],occidx]
    fvv = fock[viridx[:,None],viridx]

    h_diag = (fvv.diagonal().reshape(-1,1)-foo.diagonal()) * 2

    # To project Hessians from another basis if different basis sets are used
    # in newton solver and underlying mean-filed solver.
    if hasattr(mf, '_scf') and id(mf._scf.mol) != id(mol):
        mo_coeff = addons.project_mo_nr2nr(mf._scf.mol, mo_coeff, mol)

    if hasattr(mf, 'xc') and hasattr(mf, '_numint'):
        if mf.grids.coords is None:
            mf.grids.build()
        ni = mf._numint
        hyb = ni.hybrid_coeff(mf.xc, spin=(mol.spin>0)+1)
        rho0, vxc, fxc = ni.cache_xc_kernel(mol, mf.grids, mf.xc, mo_coeff, mo_occ, 0)
        dm0 = None #mf.make_rdm1(mo_coeff, mo_occ)
    else:
        hyb = None

    def h_op(x):
        x = x.reshape(nvir,nocc)
        x2 = numpy.einsum('sp,sq->pq', fvv, x.conj()) * 2
        x2-= numpy.einsum('sp,rp->rs', foo, x.conj()) * 2

        d1 = reduce(numpy.dot, (mo_coeff[:,viridx], x, mo_coeff[:,occidx].T.conj()))
        dm1 = d1 + d1.T.conj()
        if hyb is None:
            vj, vk = mf.get_jk(mol, dm1)
            v1 = vj - vk * .5
        else:
            v1 = ni.nr_rks_fxc(mol, mf.grids, mf.xc, dm0, dm1, 0, 1, rho0, vxc, fxc)
## *.7 this magic number can stablize DFT convergence.  Without scaling down
## the XC hessian, large oscillation is observed in Newton iterations.  It may
## be due to the numerical integration error.  Scaling down XC hessian can
## reduce to some extent the integration error.
#            v1 *= .7
            if abs(hyb) < 1e-10:
                v1 += mf.get_j(mol, dm1)
            else:
                vj, vk = mf.get_jk(mol, dm1)
                v1 += vj - vk * (hyb * .5)
        x2 += reduce(numpy.dot, (mo_coeff[:,occidx].T.conj(), v1,
                                 mo_coeff[:,viridx])).T * 4
        return x2.reshape(-1)

    return g.reshape(-1), h_op, h_diag.reshape(-1)

def gen_g_hop_rohf(mf, mo_coeff, mo_occ, fock_ao=None, h1e=None):
    mo_occa = occidxa = mo_occ > 0
    mo_occb = occidxb = mo_occ ==2
    ug, uh_op, uh_diag = gen_g_hop_uhf(mf, (mo_coeff,)*2, (mo_occa,mo_occb), fock_ao)

    viridxa = ~occidxa
    viridxb = ~occidxb
    uniq_var_a = viridxa.reshape(-1,1) & occidxa
    uniq_var_b = viridxb.reshape(-1,1) & occidxb
    uniq_ab = uniq_var_a | uniq_var_b
    nmo = mo_coeff.shape[-1]
    nocca = mo_occa.sum()
    nvira = nmo - nocca

    def sum_ab(x):
        x1 = numpy.zeros((nmo,nmo))
        x1[uniq_var_a]  = x[:nvira*nocca]
        x1[uniq_var_b] += x[nvira*nocca:]
        return x1[uniq_ab]

    g = sum_ab(ug)
    h_diag = sum_ab(uh_diag)
    def h_op(x):
        x1 = numpy.zeros((nmo,nmo))
        # unpack ROHF rotation parameters
        x1[uniq_ab] = x
        x1 = numpy.hstack((x1[uniq_var_a],x1[uniq_var_b]))
        return sum_ab(uh_op(x1))

    return g, h_op, h_diag

def gen_g_hop_uhf(mf, mo_coeff, mo_occ, fock_ao=None, h1e=None):
    mol = mf.mol
    occidxa = numpy.where(mo_occ[0]>0)[0]
    occidxb = numpy.where(mo_occ[1]>0)[0]
    viridxa = numpy.where(mo_occ[0]==0)[0]
    viridxb = numpy.where(mo_occ[1]==0)[0]
    nocca = len(occidxa)
    noccb = len(occidxb)
    nvira = len(viridxa)
    nvirb = len(viridxb)

    if fock_ao is None:
        if h1e is None: h1e = mf.get_hcore()
        dm0 = mf.make_rdm1(mo_coeff, mo_occ)
        fock_ao = h1e + mf.get_veff(mol, dm0)
    focka = reduce(numpy.dot, (mo_coeff[0].T, fock_ao[0], mo_coeff[0]))
    fockb = reduce(numpy.dot, (mo_coeff[1].T, fock_ao[1], mo_coeff[1]))

    g = numpy.hstack((focka[occidxa[:,None],viridxa].T.ravel(),
                      fockb[occidxb[:,None],viridxb].T.ravel()))

    h_diaga =(focka[viridxa,viridxa].reshape(-1,1) - focka[occidxa,occidxa])
    h_diagb =(fockb[viridxb,viridxb].reshape(-1,1) - fockb[occidxb,occidxb])
    h_diag = numpy.hstack((h_diaga.reshape(-1), h_diagb.reshape(-1)))

    if hasattr(mf, '_scf') and id(mf._scf.mol) != id(mol):
        mo_coeff = (addons.project_mo_nr2nr(mf._scf.mol, mo_coeff[0], mol),
                    addons.project_mo_nr2nr(mf._scf.mol, mo_coeff[1], mol))

    if hasattr(mf, 'xc') and hasattr(mf, '_numint'):
        if mf.grids.coords is None:
            mf.grids.build()
        ni = mf._numint
        hyb = ni.hybrid_coeff(mf.xc, spin=(mol.spin>0)+1)
        rho0, vxc, fxc = ni.cache_xc_kernel(mol, mf.grids, mf.xc, mo_coeff, mo_occ, 1)
        #dm0 =(numpy.dot(mo_coeff[0]*mo_occ[0], mo_coeff[0].T.conj()),
        #      numpy.dot(mo_coeff[1]*mo_occ[1], mo_coeff[1].T.conj()))
        dm0 = None
    else:
        hyb = None

    def h_op(x):
        x1a = x[:nvira*nocca].reshape(nvira,nocca)
        x1b = x[nvira*nocca:].reshape(nvirb,noccb)
        x2a = numpy.einsum('rp,rq->pq', focka[viridxa[:,None],viridxa], x1a.conj())
        x2a-= numpy.einsum('sq,ps->pq', focka[occidxa[:,None],occidxa], x1a.conj())
        x2b = numpy.einsum('rp,rq->pq', fockb[viridxb[:,None],viridxb], x1b.conj())
        x2b-= numpy.einsum('sq,ps->pq', fockb[occidxb[:,None],occidxb], x1b.conj())

        d1a = reduce(numpy.dot, (mo_coeff[0][:,viridxa], x1a,
                                 mo_coeff[0][:,occidxa].T.conj()))
        d1b = reduce(numpy.dot, (mo_coeff[1][:,viridxb], x1b,
                                 mo_coeff[1][:,occidxb].T.conj()))
        dm1 = numpy.array((d1a+d1a.T.conj(),d1b+d1b.T.conj()))
        if hyb is None:
            vj, vk = mf.get_jk(mol, dm1)
            v1 = vj[0]+vj[1] - vk
        else:
            v1 = ni.nr_uks_fxc(mol, mf.grids, mf.xc, dm0, dm1, 0, 1, rho0, vxc, fxc)
## *.7 this magic number can stablize DFT convergence.  Without scaling down
## the XC hessian, large oscillation is observed in Newton iterations.  It may
## be due to the numerical integration error.  Scaling down XC hessian can
## reduce to some extent the integration error.
#            v1 *= .7
            if abs(hyb) < 1e-10:
                vj = mf.get_j(mol, dm1)
                v1 += vj[0] + vj[1]
            else:
                vj, vk = mf.get_jk(mol, dm1)
                v1 += vj[0]+vj[1] - vk * hyb
        x2a += reduce(numpy.dot, (mo_coeff[0][:,occidxa].T.conj(), v1[0],
                                  mo_coeff[0][:,viridxa])).T
        x2b += reduce(numpy.dot, (mo_coeff[1][:,occidxb].T.conj(), v1[1],
                                  mo_coeff[1][:,viridxb])).T
        return numpy.hstack((x2a.ravel(), x2b.ravel()))

    return g, h_op, h_diag


def project_mol(mol, projectbasis={}):
    from pyscf import df
    uniq_atoms = set([a[0] for a in mol._atom])
    newbasis = {}
    for symb in uniq_atoms:
        if gto.mole._charge(symb) <= 10:
            newbasis[symb] = '321g'
        elif gto.mole._charge(symb) <= 12:
            newbasis[symb] = 'dzp'
        elif gto.mole._charge(symb) <= 18:
            newbasis[symb] = 'dz'
        elif gto.mole._charge(symb) <= 86:
            newbasis[symb] = 'dzp'
        else:
            newbasis[symb] = 'sto3g'
    if isinstance(projectbasis, (dict, tuple, list)):
        newbasis.update(projectbasis)
    elif isinstance(projectbasis, str):
        for k in newbasis:
            newbasis[k] = projectbasis
    return df.format_aux_basis(mol, newbasis)


# TODO: check whether high order terms in (g_orb, h_op) affects optimization
# To include high order terms, we can generate mo_coeff every time u matrix
# changed and insert the mo_coeff to g_op, h_op.
# Seems the high order terms do not help optimization?
def rotate_orb_cc(mf, mo_coeff, mo_occ, fock_ao, h1e,
                  conv_tol_grad=None, verbose=None):
    from pyscf.scf import ciah
    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(mf.stdout, mf.verbose)

    if conv_tol_grad is None:
        conv_tol_grad = numpy.sqrt(mf.conv_tol*.1)

    t2m = (time.clock(), time.time())
    if isinstance(mo_coeff, numpy.ndarray) and mo_coeff.ndim == 2:
        nmo = mo_coeff.shape[-1]
    else:
        nmo = mo_coeff[0].shape[-1]
    g_orb, h_op, h_diag = mf.gen_g_hop(mo_coeff, mo_occ, fock_ao)
    g_kf = g_orb
    norm_gkf = norm_gorb = numpy.linalg.norm(g_orb)
    log.debug('    |g|= %4.3g (keyframe)', norm_gorb)
    t3m = log.timer('gen h_op', *t2m)

    def precond(x, e):
        hdiagd = h_diag-(e-mf.ah_level_shift)
        hdiagd[abs(hdiagd)<1e-8] = 1e-8
        x = x/hdiagd
## Because of DFT, donot norm to 1 which leads 1st DM too large.
#        norm_x = numpy.linalg.norm(x)
#        if norm_x < 1e-2:
#            x *= 1e-2/norm_x
        return x

    kf_trust_region = mf.kf_trust_region
    g_op = lambda: g_orb
    x0_guess = g_orb
    while True:
        ah_conv_tol = min(norm_gorb**2, mf.ah_conv_tol)
        # increase the AH accuracy when approach convergence
        ah_start_tol = min(norm_gorb*5, mf.ah_start_tol)
        #ah_start_cycle = max(mf.ah_start_cycle, int(-numpy.log10(norm_gorb)))
        ah_start_cycle = mf.ah_start_cycle
        g_orb0 = g_orb
        imic = 0
        dr = 0
        ukf = None
        jkcount = 0
        kfcount = 0
        ikf = 0
        dm0 = mf.make_rdm1(mo_coeff, mo_occ)
        vhf0 = fock_ao - h1e

        for ah_end, ihop, w, dxi, hdxi, residual, seig \
                in ciah.davidson_cc(h_op, g_op, precond, x0_guess,
                                    tol=ah_conv_tol, max_cycle=mf.ah_max_cycle,
                                    lindep=mf.ah_lindep, verbose=log):
            norm_residual = numpy.linalg.norm(residual)
            if (ah_end or ihop == mf.ah_max_cycle or # make sure to use the last step
                ((norm_residual < ah_start_tol) and (ihop >= ah_start_cycle)) or
                (seig < mf.ah_lindep)):
                imic += 1
                dxmax = numpy.max(abs(dxi))
                if dxmax > mf.max_stepsize:
                    scale = mf.max_stepsize / dxmax
                    log.debug1('... scale rotation size %g', scale)
                    dxi *= scale
                    hdxi *= scale
                else:
                    scale = None

                dr = dr + dxi
                g_orb = g_orb + hdxi
                norm_dr = numpy.linalg.norm(dr)
                norm_gorb = numpy.linalg.norm(g_orb)
                norm_dxi = numpy.linalg.norm(dxi)
                log.debug('    imic %d(%d)  |g|= %4.3g  |dxi|= %4.3g  '
                          'max(|x|)= %4.3g  |dr|= %4.3g  eig= %4.3g  seig= %4.3g',
                          imic, ihop, norm_gorb, norm_dxi,
                          dxmax, norm_dr, w, seig)

                max_cycle = max(mf.max_cycle_inner,
                                mf.max_cycle_inner-int(numpy.log(norm_gkf+1e-9)*2))
                log.debug1('Set ah_start_tol %g, ah_start_cycle %d, max_cycle %d',
                           ah_start_tol, ah_start_cycle, max_cycle)
                ikf += 1
                if imic > 3 and norm_gorb > norm_gkf*mf.ah_grad_trust_region:
                    g_orb = g_orb - hdxi
                    dr -= dxi
                    norm_gorb = numpy.linalg.norm(g_orb)
                    log.debug('|g| >> keyframe, Restore previouse step')
                    break

                elif (imic >= max_cycle or norm_gorb < conv_tol_grad*.5):
                    break

                elif (ikf > 2 and # avoid frequent keyframe
#TODO: replace it with keyframe_scheduler
                      (ikf >= max(mf.kf_interval, mf.kf_interval-numpy.log(norm_dr+1e-9)) or
# Insert keyframe if the keyframe and the esitimated g_orb are too different
                       norm_gorb < norm_gkf/kf_trust_region)):
                    ikf = 0
                    u = mf.update_rotate_matrix(dr, mo_occ)
                    if ukf is not None:
                        u = mf.rotate_mo(ukf, u)
                    ukf = u
                    dr[:] = 0
                    mo1 = mf.rotate_mo(mo_coeff, u)
                    dm = mf.make_rdm1(mo1, mo_occ)
# use mf._scf.get_veff to avoid density-fit mf polluting get_veff
                    vhf0 = mf._scf.get_veff(mf._scf.mol, dm, dm_last=dm0, vhf_last=vhf0)
                    kfcount += 1
                    dm0 = dm
                    g_kf1 = mf.get_grad(mo1, mo_occ, h1e+vhf0)
                    norm_gkf1 = numpy.linalg.norm(g_kf1)
                    norm_dg = numpy.linalg.norm(g_kf1-g_orb)
                    jkcount += 1
                    log.debug('Adjust keyframe g_orb to |g|= %4.3g  '
                              '|g-correction|= %4.3g', norm_gkf1, norm_dg)
                    if (norm_dg < norm_gorb*mf.ah_grad_trust_region  # kf not too diff
                        #or norm_gkf1 < norm_gkf  # grad is decaying
                        # close to solution
                        or norm_gkf1 < conv_tol_grad*mf.ah_grad_trust_region):
                        kf_trust_region = min(max(norm_gorb/(norm_dg+1e-9), mf.kf_trust_region), 10)
                        log.debug1('Set kf_trust_region = %g', kf_trust_region)
                        g_orb = g_kf = g_kf1
                        norm_gorb = norm_gkf = norm_gkf1
                    else:
                        g_orb = g_orb - hdxi
                        dr -= dxi
                        norm_gorb = numpy.linalg.norm(g_orb)
                        log.debug('Out of trust region. Restore previouse step')
                        break


        u = mf.update_rotate_matrix(dr, mo_occ)
        if ukf is not None:
            u = mf.rotate_mo(ukf, u)
        jkcount += ihop + 1
        log.debug('    tot inner=%d  %d JK  |g|= %4.3g  |u-1|= %4.3g',
                  imic, jkcount, norm_gorb,
                  numpy.linalg.norm(u-numpy.eye(nmo)))
        h_op = h_diag = None
        t3m = log.timer('aug_hess in %d inner iters' % imic, *t3m)
        mo_coeff, mo_occ, fock_ao = (yield u, g_kf, kfcount, jkcount)

        g_kf, h_op, h_diag = mf.gen_g_hop(mo_coeff, mo_occ, fock_ao)
        norm_gkf = numpy.linalg.norm(g_kf)
        norm_dg = numpy.linalg.norm(g_kf-g_orb)
        log.debug('    |g|= %4.3g (keyframe), |g-correction|= %4.3g',
                  norm_gkf, norm_dg)
        kf_trust_region = min(max(norm_gorb/(norm_dg+1e-9), mf.kf_trust_region), 10)
        log.debug1('Set  kf_trust_region = %g', kf_trust_region)
        g_orb = g_kf
        norm_gorb = norm_gkf
        x0_guess = dxi


def kernel(mf, mo_coeff, mo_occ, conv_tol=1e-10, conv_tol_grad=None,
           max_cycle=50, dump_chk=True,
           callback=None, verbose=logger.NOTE):
    cput0 = (time.clock(), time.time())
    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(mf.stdout, verbose)
    mol = mf._scf.mol
    if conv_tol_grad is None:
        conv_tol_grad = numpy.sqrt(conv_tol)
        log.info('Set conv_tol_grad to %g', conv_tol_grad)
    scf_conv = False
    e_tot = mf.e_tot

# call mf._scf.get_hcore, mf._scf.get_ovlp because they might be overloaded
    h1e = mf._scf.get_hcore(mol)
    s1e = mf._scf.get_ovlp(mol)
    dm = mf.make_rdm1(mo_coeff, mo_occ)
# call mf._scf.get_veff, to avoid density_fit module polluting get_veff function
    vhf = mf._scf.get_veff(mol, dm)
    fock = mf.get_fock(h1e, s1e, vhf, dm, 0, None)
    log.info('Initial guess |g|= %g',
             numpy.linalg.norm(mf._scf.get_grad(mo_coeff, mo_occ, fock)))
# NOTE: DO NOT change the initial guess mo_occ, mo_coeff
    mo_energy, mo_tmp = mf.eig(fock, s1e)
    mf.get_occ(mo_energy, mo_tmp)

    if dump_chk:
        chkfile.save_mol(mol, mf.chkfile)

    rotaiter = rotate_orb_cc(mf, mo_coeff, mo_occ, fock, h1e, conv_tol_grad, log)
    u, g_orb, kfcount, jkcount = next(rotaiter)
    kftot = kfcount + 1
    jktot = jkcount
    cput1 = log.timer('initializing second order scf', *cput0)

    for imacro in range(max_cycle):
        dm_last = dm
        last_hf_e = e_tot
        norm_gorb = numpy.linalg.norm(g_orb)
        mo_coeff = mf.rotate_mo(mo_coeff, u, log)
        dm = mf.make_rdm1(mo_coeff, mo_occ)
        vhf = mf._scf.get_veff(mol, dm, dm_last=dm_last, vhf_last=vhf)
        fock = mf.get_fock(h1e, s1e, vhf, dm, imacro, None)
# NOTE: DO NOT change the initial guess mo_occ, mo_coeff
        if mf.verbose >= logger.DEBUG:
            mo_energy, mo_tmp = mf.eig(fock, s1e)
            mf.get_occ(mo_energy, mo_tmp)
# call mf._scf.energy_tot for dft, because the (dft).get_veff step saved _exc in mf._scf
        e_tot = mf._scf.energy_tot(dm, h1e, vhf)

        log.info('macro= %d  E= %.15g  delta_E= %g  |g|= %g  %d KF %d JK',
                 imacro, e_tot, e_tot-last_hf_e, norm_gorb,
                 kfcount+1, jkcount)
        cput1 = log.timer('cycle= %d'%(imacro+1), *cput1)

        if (abs((e_tot-last_hf_e)/e_tot)*1e2 < conv_tol and
            norm_gorb < conv_tol_grad):
            scf_conv = True

        if dump_chk:
            mf.dump_chk(locals())

        if callable(callback):
            callback(locals())

        if scf_conv:
            break

        u, g_orb, kfcount, jkcount = rotaiter.send((mo_coeff, mo_occ, fock))
        kftot += kfcount + 1
        jktot += jkcount

    if callable(callback):
        callback(locals())

    rotaiter.close()
    if mf.canonicalization:
        log.info('Canonicalize SCF orbitals')
        mo_energy, mo_coeff = mf._scf.canonicalize(mo_coeff, mo_occ, fock)
        mo_occ = mf._scf.get_occ(mo_energy, mo_coeff)
    else:
        mo_energy = mf._scf.canonicalize(mo_coeff, mo_occ, fock)[0]
    log.info('macro X = %d  E=%.15g  |g|= %g  total %d KF %d JK',
             imacro+1, e_tot, norm_gorb, kftot, jktot)
    return scf_conv, e_tot, mo_energy, mo_coeff, mo_occ

# Note which function that "density_fit" decorated.  density-fit have been
# considered separatedly for newton_mf and newton_mf._scf, there are 3 cases
# 1. both grad and hessian by df, because the input mf obj is decorated by df
#       newton(scf.density_fit(scf.RHF(mol)))
# 2. both grad and hessian in df, because both newton_mf and the input mf objs
#    are decorated by df
#       scf.density_fit(newton(scf.density_fit(scf.RHF(mol))))
# 3. grad by explicit scheme, hessian by df, because only newton_mf obj is
#    decorated by df
#       scf.density_fit(newton(scf.RHF(mol)))
# The following function is not necessary
#def density_fit_(mf, auxbasis='weigend+etb'):
#    mfaux = mf.density_fit(auxbasis)
#    mf.gen_g_hop = mfaux.gen_g_hop
#    return mf
#def density_fit(mf, auxbasis='weigend+etb'):
#    return density_fit_(copy.copy(mf), auxbasis)


def newton_SCF_class(mf):
    '''Generate the CIAH base class
    '''
    from pyscf import scf
    if mf.__class__.__doc__ is None:
        doc = ''
    else:
        doc = mf.__class__.__doc__
    class CIAH_SCF(mf.__class__):
        __doc__ = doc + \
        '''
        Attributes for Newton solver:
            max_cycle_inner : int
                AH iterations within eacy macro iterations. Default is 10
            max_stepsize : int
                The step size for orbital rotation.  Small step is prefered.  Default is 0.05.
            canonicalization : bool
                To control whether to canonicalize the orbitals optimized by
                Newton solver.  Default is True.
        '''
        def __init__(self):
            self._scf = mf
            self.max_cycle_inner = 20
            self.max_stepsize = .05
            self.canonicalization = True

            self.ah_start_tol = 5.
            self.ah_start_cycle = 1
            self.ah_level_shift = 0
            self.ah_conv_tol = 1e-12
            self.ah_lindep = 1e-14
            self.ah_max_cycle = 40
            self.ah_grad_trust_region = 2.5
# * Classic AH can be simulated by setting
#               max_cycle_micro_inner = 1
#               ah_start_tol = 1e-7
#               max_stepsize = 1.5
#               ah_grad_trust_region = 1e6
            self.kf_interval = 5
            self.kf_trust_region = 5
            self_keys = set(self.__dict__.keys())

# Note self.mol can be different to self._scf.mol.  Projected hessian is used
# in this case.
            self.__dict__.update(mf.__dict__)
            self._keys = self_keys.union(mf._keys)

        def dump_flags(self):
            log = logger.Logger(self.stdout, self.verbose)
            log.info('\n')
            self._scf.dump_flags()
            log.info('******** %s Newton solver flags ********', self._scf.__class__)
            log.info('SCF tol = %g', self.conv_tol)
            log.info('conv_tol_grad = %s',    self.conv_tol_grad)
            log.info('max. SCF cycles = %d', self.max_cycle)
            log.info('direct_scf = %s', self._scf.direct_scf)
            if self._scf.direct_scf:
                log.info('direct_scf_tol = %g', self._scf.direct_scf_tol)
            if self.chkfile:
                log.info('chkfile to save SCF result = %s', self.chkfile)
            log.info('max_cycle_inner = %d',  self.max_cycle_inner)
            log.info('max_stepsize = %g', self.max_stepsize)
            log.info('ah_start_tol = %g',     self.ah_start_tol)
            log.info('ah_level_shift = %g',   self.ah_level_shift)
            log.info('ah_conv_tol = %g',      self.ah_conv_tol)
            log.info('ah_lindep = %g',        self.ah_lindep)
            log.info('ah_start_cycle = %d',   self.ah_start_cycle)
            log.info('ah_max_cycle = %d',     self.ah_max_cycle)
            log.info('ah_grad_trust_region = %g', self.ah_grad_trust_region)
            log.info('kf_interval = %d', self.kf_interval)
            log.info('kf_trust_region = %d', self.kf_trust_region)
            log.info('max_memory %d MB (current use %d MB)',
                     self.max_memory, lib.current_memory()[0])

        def get_fock(self, h1e, s1e, vhf, dm, cycle=-1, diis=None,
                     diis_start_cycle=None, level_shift_factor=None,
                     damp_factor=None):
            return h1e + vhf

        def build(self, mol=None):
            if mol is None: mol = self.mol
            if self.verbose >= logger.WARN:
                self.check_sanity()
            if hasattr(self, '_scf') and id(self._scf.mol) != id(mol):
                self.opt = self.init_direct_scf(mol)
                self._eri = None

        def kernel(self, mo_coeff=None, mo_occ=None):
            if mo_coeff is None:
                mo_coeff = self.mo_coeff
            if mo_occ is None:
                mo_occ = self.mo_occ
            cput0 = (time.clock(), time.time())

            self.build(self.mol)
            self.dump_flags()

            if mo_coeff is None or mo_occ is None:
                logger.debug(self, 'Initial guess orbitals not given. '
                             'Generating initial guess from %s density matrix',
                             self.init_guess)
                dm = mf.get_init_guess(self.mol, self.init_guess)
                mo_coeff, mo_occ = self.from_dm(dm)
            # save initial guess because some methods may access them
            self.mo_coeff = mo_coeff
            self.mo_occ = mo_occ

            self.converged, self.e_tot, \
                    self.mo_energy, self.mo_coeff, self.mo_occ = \
                    kernel(self, mo_coeff, mo_occ, conv_tol=self.conv_tol,
                           conv_tol_grad=self.conv_tol_grad,
                           max_cycle=self.max_cycle,
                           callback=self.callback, verbose=self.verbose)

            logger.timer(self, 'Second order SCF', *cput0)
            self._finalize()
            return self.e_tot

        def from_dm(self, dm):
            '''Transform density matrix to the initial guess'''
            if isinstance(dm, str):
                sys.stderr.write('Newton solver reads density matrix from chkfile %s\n' % dm)
                dm = self.from_chk(dm, True)
            mol = self._scf.mol
            h1e = self._scf.get_hcore(mol)
            s1e = self._scf.get_ovlp(mol)
            vhf = self._scf.get_veff(mol, dm)
            fock = self._scf.get_fock(h1e, s1e, vhf, dm, 0, None)
            mo_energy, mo_coeff = self._scf.eig(fock, s1e)
            mo_occ = self._scf.get_occ(mo_energy, mo_coeff)
            return mo_coeff, mo_occ

        gen_g_hop = gen_g_hop_rhf

        def update_rotate_matrix(self, dx, mo_occ, u0=1):
            dr = scf.hf.unpack_uniq_var(dx, mo_occ)
            return numpy.dot(u0, expmat(dr))

        def rotate_mo(self, mo_coeff, u, log=None):
            return numpy.dot(mo_coeff, u)
    return CIAH_SCF

def newton(mf):
    '''Co-iterative augmented hessian (CIAH) second order SCF solver

    Examples:

    >>> mol = gto.M(atom='H 0 0 0; H 0 0 1.1', basis='cc-pvdz')
    >>> mf = scf.RHF(mol).run(conv_tol=.5)
    >>> mf = scf.newton(mf).set(conv_tol=1e-9)
    >>> mf.kernel()
    -1.0811707843774987
    '''
    from pyscf import scf
    from pyscf.mcscf import mc1step_symm

    SCF = newton_SCF_class(mf)
    class RHF(SCF):
        def gen_g_hop(self, mo_coeff, mo_occ, fock_ao=None, h1e=None):
            mol = self._scf.mol
            if mol.symmetry:
# label the symmetry every time calling gen_g_hop because kernel function may
# change mo_coeff ordering after calling self.eig
                self._orbsym = symm.label_orb_symm(mol, mol.irrep_id,
                        mol.symm_orb, mo_coeff, s=self._scf.get_ovlp())
            return gen_g_hop_rhf(self, mo_coeff, mo_occ, fock_ao, h1e)

        def update_rotate_matrix(self, dx, mo_occ, u0=1):
            dr = scf.hf.unpack_uniq_var(dx, mo_occ)
            if self._scf.mol.symmetry:
                dr = mc1step_symm._symmetrize(dr, self._orbsym, None)
            return numpy.dot(u0, expmat(dr))

        def rotate_mo(self, mo_coeff, u, log=None):
            #from pyscf.lo import orth
            #s = self._scf.get_ovlp()
            #if self._scf.mol.symmetry:
            #    s = symm.symmetrize_matrix(s, self._scf._scf.mol.symm_orb)
            #return orth.vec_lowdin(numpy.dot(mo_coeff, u), s)
            mo = numpy.dot(mo_coeff, u)
            if log is not None and log.verbose >= logger.DEBUG:
                idx = numpy.where(self.mo_occ > 0)[0]
                s = reduce(numpy.dot, (mo[:,idx].T, self._scf.get_ovlp(),
                                       self.mo_coeff[:,idx]))
                log.debug('Overlap to initial guess, SVD = %s',
                          _effective_svd(s, 1e-5))
                log.debug('Overlap to last step, SVD = %s',
                          _effective_svd(lib.take_2d(u,idx,idx), 1e-5))
            return mo

    if isinstance(mf, scf.rohf.ROHF):
        class ROHF(RHF):
            def gen_g_hop(self, mo_coeff, mo_occ, fock_ao=None, h1e=None):
                mol = self._scf.mol
                if mol.symmetry:
# label the symmetry every time calling gen_g_hop because kernel function may
# change mo_coeff ordering after calling self.eig
                    self._orbsym = symm.label_orb_symm(mol, mol.irrep_id,
                            mol.symm_orb, mo_coeff, s=self._scf.get_ovlp())
                return gen_g_hop_rohf(self, mo_coeff, mo_occ, fock_ao, h1e)

            def get_fock(self, h1e, s1e, vhf, dm, cycle=-1, diis=None,
                         diis_start_cycle=None, level_shift_factor=None,
                         damp_factor=None):
                fock = h1e + vhf
                self._focka_ao = self._scf._focka_ao = fock[0]  # needed by ._scf.eig
                self._fockb_ao = self._scf._fockb_ao = fock[1]  # needed by ._scf.eig
                self._dm_ao = dm  # needed by .eig
                return fock

            def eig(self, fock, s1e):
                f = (self._focka_ao, self._fockb_ao)
                f = scf.rohf.get_roothaan_fock(f, self._dm_ao, s1e)
                return self._scf.eig(f, s1e)
                #fc = numpy.dot(fock[0], mo_coeff)
                #mo_energy = numpy.einsum('pk,pk->k', mo_coeff, fc)
                #return mo_energy
        return ROHF()

    elif isinstance(mf, scf.uhf.UHF):
        class UHF(SCF):
            def gen_g_hop(self, mo_coeff, mo_occ, fock_ao=None, h1e=None):
                mol = self._scf.mol
                if mol.symmetry:
                    ovlp_ao = self._scf.get_ovlp()
                    self._orbsym =(symm.label_orb_symm(mol, mol.irrep_id,
                                            mol.symm_orb, mo_coeff[0], s=ovlp_ao),
                                   symm.label_orb_symm(mol, mol.irrep_id,
                                            mol.symm_orb, mo_coeff[1], s=ovlp_ao))
                return gen_g_hop_uhf(self, mo_coeff, mo_occ, fock_ao, h1e)

            def update_rotate_matrix(self, dx, mo_occ, u0=1):
                occidxa = mo_occ[0] > 0
                occidxb = mo_occ[1] > 0
                viridxa = ~occidxa
                viridxb = ~occidxb
                nmo = len(occidxa)
                nocca = occidxa.sum()
                nvira = nmo - nocca

                dra = numpy.zeros((nmo,nmo))
                drb = numpy.zeros((nmo,nmo))
                dra[viridxa[:,None]&occidxa] = dx[:nocca*nvira]
                drb[viridxb[:,None]&occidxb] = dx[nocca*nvira:]
                dra = dra - dra.T
                drb = drb - drb.T

                if self._scf.mol.symmetry:
                    dra = mc1step_symm._symmetrize(dra, self._orbsym[0], None)
                    drb = mc1step_symm._symmetrize(drb, self._orbsym[1], None)
                if isinstance(u0, int) and u0 == 1:
                    return numpy.asarray((expmat(dra), expmat(drb)))
                else:
                    return numpy.asarray((numpy.dot(u0[0], expmat(dra)),
                                          numpy.dot(u0[1], expmat(drb))))

            def rotate_mo(self, mo_coeff, u, log=None):
                return numpy.asarray((numpy.dot(mo_coeff[0], u[0]),
                                      numpy.dot(mo_coeff[1], u[1])))

            def spin_square(self, mo_coeff=None, s=None):
                if mo_coeff is None:
                    mo_coeff = (self.mo_coeff[0][:,self.mo_occ[0]>0],
                                self.mo_coeff[1][:,self.mo_occ[1]>0])
                if hasattr(self, '_scf') and id(self._scf.mol) != id(self.mol):
                    s = self._scf.get_ovlp()
                return self._scf.spin_square(mo_coeff, s)
        return UHF()

    elif isinstance(mf, scf.dhf.UHF):
        raise RuntimeError('Not support Dirac-HF')

    else:
        return RHF()

def _effective_svd(a, tol=1e-5):
    w = numpy.linalg.svd(a)[1]
    return w[(tol<w) & (w<1-tol)]


if __name__ == '__main__':
    from pyscf import scf
    import pyscf.fci
    from pyscf.mcscf import addons

    mol = gto.Mole()
    mol.verbose = 0
    mol.atom = [
        ['H', ( 1.,-1.    , 0.   )],
        ['H', ( 0.,-1.    ,-1.   )],
        ['H', ( 1.,-0.5   ,-1.   )],
        ['H', ( 0., 1.    , 1.   )],
        ['H', ( 0., 0.5   , 1.   )],
        ['H', ( 1., 0.    ,-1.   )],
    ]

    mol.basis = '6-31g'
    mol.build()

    nmo = mol.nao_nr()
    m = newton(scf.RHF(mol))
    e0 = m.kernel()

#####################################
    mol.basis = '6-31g'
    mol.spin = 2
    mol.build(0, 0)
    m = scf.RHF(mol)
    m.max_cycle = 1
    #m.verbose = 5
    m.scf()
    e1 = kernel(newton(m), m.mo_coeff, m.mo_occ, max_cycle=50, verbose=5)[1]

    m = scf.UHF(mol)
    m.max_cycle = 1
    #m.verbose = 5
    m.scf()
    e2 = kernel(newton(m), m.mo_coeff, m.mo_occ, max_cycle=50, verbose=5)[1]

    m = scf.UHF(mol)
    m.max_cycle = 1
    #m.verbose = 5
    m.scf()
    nrmf = scf.density_fit(newton(m))
    nrmf.max_cycle = 50
    nrmf.conv_tol = 1e-8
    nrmf.conv_tol_grad = 1e-5
    #nrmf.verbose = 5
    e4 = nrmf.kernel()

    m = scf.density_fit(scf.UHF(mol))
    m.max_cycle = 1
    #m.verbose = 5
    m.scf()
    nrmf = scf.density_fit(newton(m))
    nrmf.max_cycle = 50
    nrmf.conv_tol_grad = 1e-5
    e5 = nrmf.kernel()

    print(e0 - -2.93707955256)
    print(e1 - -2.99456398848)
    print(e2 - -2.99663808314)
    print(e4 - -2.99663808186)
    print(e5 - -2.99634506072)
