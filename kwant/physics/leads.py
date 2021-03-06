# Copyright 2011-2013 Kwant authors.
#
# This file is part of Kwant.  It is subject to the license terms in the file
# LICENSE.rst found in the top-level directory of this distribution and at
# http://kwant-project.org/license.  A list of Kwant authors can be found in
# the file AUTHORS.rst at the top-level directory of this distribution and at
# http://kwant-project.org/authors.


from math import sin, cos, sqrt, pi, copysign
from collections import namedtuple

import numpy as np
import numpy.linalg as npl
import scipy.linalg as la
from .. import linalg as kla

dot = np.dot

__all__ = ['selfenergy', 'modes', 'PropagatingModes', 'StabilizedModes']


if np.__version__ >= '1.8':
    complex_any = np.any
else:
    def complex_any(array):
        """Check if a complex array has nonzero entries.

        This function is needed due to a bug in numpy<1.8.
        """
        # TODO: Remove separate checking of real and imaginary parts once we depend
        # on numpy>=1.8 (it is present due to a bug in earlier versions).
        return np.any(array.real) or np.any(array.imag)


# Container classes
Linsys = namedtuple('Linsys', ['eigenproblem', 'v', 'extract'])


class PropagatingModes:
    """The calculated propagating modes of a lead.

    Attributes
    ----------
    wave_functions : numpy array
        The wave functions of the propagating modes.
    momenta : numpy array
        Momenta of the modes.
    velocities : numpy array
        Velocities of the modes.

    Notes
    =====
    The sort order of all the three arrays is identical. The first half of the
    modes have negative velocity, the second half have positive velocity. The
    modes with negative velocity are ordered from larger to lower momenta, the
    modes with positive velocity vice versa.

    The first dimension of `wave_functions` corresponds to the orbitals of all
    the sites in a unit cell, the second one to the number of the mode.  Each
    mode is normalized to carry unit current. If several modes have the same
    momentum and velocity, an arbitrary orthonormal basis in the subspace of
    these modes is chosen.
    """
    def __init__(self, wave_functions, velocities, momenta):
        kwargs = locals()
        kwargs.pop('self')
        self.__dict__.update(kwargs)


class StabilizedModes:
    """Stabilized eigendecomposition of the translation operator.

    Due to the lack of Hermiticity of the translation operator, its
    eigendecomposition is frequently poorly conditioned. Solvers in Kwant use
    this stabilized decomposition of the propagating and evanescent modes in
    the leads. If the hopping between the unit cells of an infinite system is
    invertible, the translation eigenproblem is written in the basis `psi_n,
    h_hop^+ * psi_(n+1)`, with ``h_hop`` the hopping between unit cells.  If
    `h_hop` is not invertible, and has the singular value decomposition `u s
    v^+`, then the eigenproblem is written in the basis `sqrt(s) v^+ psi_n,
    sqrt(s) u^+ psi_(n+1)`. In this basis we calculate the eigenvectors of the
    propagating modes, and the Schur vectors (an orthogonal basis) in the space
    of evanescent modes.

    `vecs` and `vecslmbdainv` are the first and the second halves of the wave
    functions.  The first `nmodes` are eigenmodes moving in the negative
    direction (hence they are incoming into the system in Kwant convention),
    the second `nmodes` are eigenmodes moving in the positive direction. The
    remaining modes are the Schur vectors of the modes evanescent in the
    positive direction. Propagating modes with the same eigenvalue are
    orthogonalized, and all the propagating modes are normalized to carry unit
    current. Finally the `sqrt_hop` attribute is `v sqrt(s)`.

    Attributes
    ----------
    vecs : numpy array
        Translation eigenvectors.
    vecslmbdainv : numpy array
        Translation eigenvectors divided by the corresponding eigenvalues.
    nmodes : int
        Number of left-moving (or right-moving) modes.
    sqrt_hop : numpy array or None
        Part of the SVD of `h_hop`, or None if the latter is invertible.
    """

    def __init__(self, vecs, vecslmbdainv, nmodes, sqrt_hop=None):
        kwargs = locals()
        kwargs.pop('self')
        self.__dict__.update(kwargs)

    def selfenergy(self):
        """
        Compute the self-energy generated by lead modes.

        Returns
        -------
        Sigma : numpy array, real or complex, shape (M,M)
            The computed self-energy. Note that even if `h_cell` and `h_hop` are
            both real, `Sigma` will typically be complex. (More precisely, if
            there is a propagating mode, `Sigma` will definitely be complex.)
        """
        v = self.sqrt_hop
        vecs = self.vecs[:, self.nmodes:]
        vecslmbdainv = self.vecslmbdainv[:, self.nmodes:]
        if v is not None:
            return dot(v, dot(vecs, la.solve(vecslmbdainv, v.T.conj())))
        else:
            return la.solve(vecslmbdainv.T, vecs.T).T


# Auxiliary functions that perform different parts of the calculation.
def setup_linsys(h_cell, h_hop, tol=1e6, stabilization=None):
    """Make an eigenvalue problem for eigenvectors of translation operator.

    Parameters
    ----------
    h_cell : numpy array with shape (n, n)
        Hamiltonian of a single lead unit cell.
    h_hop : numpy array with shape (n, m), m <= n
        Hopping Hamiltonian from a cell to the next one.
    tol : float
        Numbers are considered zero when they are smaller than `tol` times
        the machine precision.
    stabilization : sequence of 2 booleans or None
        Which steps of the eigenvalue problem stabilization to perform. If the
        value is `None`, then Kwant chooses the fastest (and least stable)
        algorithm that is expected to be sufficient.  For any other value,
        Kwant forms the eigenvalue problem in the basis of the hopping singular
        values.  The first element set to `True` forces Kwant to add an
        anti-Hermitian term to the cell Hamiltonian before inverting. If it is
        set to `False`, the extra term will only be added if the cell
        Hamiltonian isn't invertible. The second element set to `True` forces
        Kwant to solve a generalized eigenvalue problem, and not to reduce it
        to the regular one.  If it is `False`, reduction to a regular problem
        is performed if possible.

    Returns
    -------
    linsys : namedtuple
        A named tuple containing `matrices` a matrix pencil defining
        the eigenproblem, `v` a hermitian conjugate of the last matrix in
        the hopping singular value decomposition, and functions for
        extracting the wave function in the unit cell from the wave function
        in the basis of the nonzero singular exponents of the hopping.

    Notes
    -----
    The lead problem with degenerate hopping is rather complicated, and the
    details of the algorithm will be published elsewhere.

    """
    n = h_cell.shape[0]
    m = h_hop.shape[1]
    if stabilization is not None:
        stabilization = list(stabilization)

    if not complex_any(h_hop):
        # Inter-cell hopping is zero.  The current algorithm is not suited to
        # treat this extremely singular case.
        raise ValueError("Inter-cell hopping is exactly zero.")

    # If both h and t are real, it may be possible to use the real eigenproblem.
    if (not np.any(h_hop.imag)) and (not np.any(h_cell.imag)):
        h_hop = h_hop.real
        h_cell = h_cell.real

    eps = np.finfo(np.common_type(h_cell, h_hop)).eps * tol

    # First check if the hopping matrix has singular values close to 0.
    # (Close to zero is defined here as |x| < eps * tol * s[0] , where
    # s[0] is the largest singular value.)

    u, s, vh = la.svd(h_hop)
    assert m == vh.shape[1], "Corrupt output of svd."
    n_nonsing = np.sum(s > eps * s[0])

    if (n_nonsing == n and stabilization is None):
        # The hopping matrix is well-conditioned and can be safely inverted.
        # Hence the regular transfer matrix may be used.
        hop_inv = la.inv(h_hop)

        A = np.zeros((2*n, 2*n), dtype=np.common_type(h_cell, h_hop))
        A[:n, :n] = dot(hop_inv, -h_cell)
        A[:n, n:] = -hop_inv
        A[n:, :n] = h_hop.T.conj()

        # The function that can extract the full wave function psi from the
        # projected one. Here it is almost trivial, but used for simplifying
        # the logic.

        def extract_wf(psi, lmbdainv):
            return np.copy(psi[:n])

        matrices = (A, None)
        v_out = None
    else:
        if stabilization is None:
            stabilization = [None, False]

        # The hopping matrix has eigenvalues close to 0 - those
        # need to be eliminated.

        # Recast the svd of h_hop = u s v^dagger such that
        # u, v are matrices with shape n x n_nonsing.
        u = u[:, :n_nonsing]
        s = s[:n_nonsing]
        u = u * np.sqrt(s)
        # pad v with zeros if necessary
        v = np.zeros((n, n_nonsing), dtype=vh.dtype)
        v[:vh.shape[1]] = vh[:n_nonsing].T.conj()
        v = v * np.sqrt(s)

        # Eliminating the zero eigenvalues requires inverting the on-site
        # Hamiltonian, possibly including a self-energy-like term.  The
        # self-energy-like term stabilizes the inversion, but the most stable
        # choice is inherently complex. This can be disadvantageous if the
        # Hamiltonian is real, since staying in real arithmetics can be
        # significantly faster.  The strategy here is to add a complex
        # self-energy-like term always if the original Hamiltonian is complex,
        # and check for invertibility first if it is real

        matrices_real = issubclass(np.common_type(h_cell, h_hop), np.floating)
        add_imaginary = stabilization[0] or ((stabilization[0] is None) and
                                             not matrices_real)
        # Check if there is a chance we will not need to add an imaginary term.
        if not add_imaginary:
            h = h_cell
            sol = kla.lu_factor(h)
            rcond = kla.rcond_from_lu(sol, npl.norm(h, 1))

            if rcond < eps:
                need_to_stabilize = True
            else:
                need_to_stabilize = False

        if add_imaginary or need_to_stabilize:
            need_to_stabilize = True
            # Matrices are complex or need self-energy-like term to be
            # stabilized.
            temp = dot(u, u.T.conj()) + dot(v, v.T.conj())
            h = h_cell + 1j * temp

            sol = kla.lu_factor(h)
            rcond = kla.rcond_from_lu(sol, npl.norm(h, 1))

            # If the condition number of the stabilized h is
            # still bad, there is nothing we can do.
            if rcond < eps:
                raise RuntimeError("Flat band encountered at the requested "
                                   "energy, result is badly defined.")

        # The function that can extract the full wave function psi from
        # the projected one (v^dagger psi lambda^-1, s u^dagger psi).

        def extract_wf(psi, lmbdainv):
            wf = -dot(u, psi[: n_nonsing] * lmbdainv) - dot(v, psi[n_nonsing:])
            if need_to_stabilize:
                wf += 1j * (dot(v, psi[: n_nonsing]) +
                            dot(u, psi[n_nonsing:] * lmbdainv))
            return kla.lu_solve(sol, wf)

        # Setup the generalized eigenvalue problem.

        A = np.zeros((2 * n_nonsing, 2 * n_nonsing), np.common_type(h, h_hop))
        B = np.zeros((2 * n_nonsing, 2 * n_nonsing), np.common_type(h, h_hop))

        begin, end = slice(n_nonsing), slice(n_nonsing, None)

        A[end, begin] = np.identity(n_nonsing)
        temp = kla.lu_solve(sol, v)
        temp2 = dot(u.T.conj(), temp)
        if need_to_stabilize:
            A[begin, begin] = -1j * temp2
        A[begin, end] = temp2
        temp2 = dot(v.T.conj(), temp)
        if need_to_stabilize:
            A[end, begin] -= 1j *temp2
        A[end, end] = temp2

        B[begin, end] = -np.identity(n_nonsing)
        temp = kla.lu_solve(sol, u)
        temp2 = dot(u.T.conj(), temp)
        B[begin, begin] = -temp2
        if need_to_stabilize:
            B[begin, end] += 1j * temp2
        temp2 = dot(v.T.conj(), temp)
        B[end, begin] = -temp2
        if need_to_stabilize:
            B[end, end] = 1j * temp2

        v_out = v[:m]

        # Solving a generalized eigenproblem is about twice as expensive
        # as solving a regular eigenvalue problem.
        # Computing the LU factorization is negligible compared to both
        # (approximately 1/30th of a regular eigenvalue problem).
        # Because of this, it makes sense to try to reduce
        # the generalized eigenvalue problem to a regular one, provided
        # the matrix B can be safely inverted.

        lu_b = kla.lu_factor(B)
        if not stabilization[1]:
            rcond = kla.rcond_from_lu(lu_b, npl.norm(B, 1))
            # A more stringent condition is used here since errors can
            # accumulate from here to the eigenvalue calculation later.
            stabilization[1] = rcond > eps * tol

        if stabilization[1]:
            matrices = (kla.lu_solve(lu_b, A), None)
        else:
            matrices = (A, B)
    return Linsys(matrices, v_out, extract_wf)


def unified_eigenproblem(a, b=None, tol=1e6):
    """A helper routine for modes(), that wraps eigenproblems.

    This routine wraps the regular and general eigenproblems that can arise
    in a unfied way.

    Parameters
    ----------
    a : numpy array
        The matrix on the left hand side of a regular or generalized eigenvalue
        problem.
    b : numpy array or None
        The matrix on the right hand side of the generalized eigenvalue problem.
    tol : float
        The tolerance for separating eigenvalues with absolute value 1 from the
        rest.

    Returns
    -------
    ev : numpy array
        An array of eigenvalues (can contain NaNs and Infs, but those
        are not accessed in `modes()`) The number of eigenvalues equals
        twice the number of nonzero singular values of
        `h_hop` (so `2*h_cell.shape[0]` if `h_hop` is invertible).
    evanselect : numpy integer array
        Index array of right-decaying modes.
    propselect : numpy integer array
        Index array of propagating modes (both left and right).
    vec_gen(select) : function
        A function that computes the eigenvectors chosen by the array select.
    ord_schur(select) : function
        A function that computes the unitary matrix (corresponding to the right
        eigenvector space) of the (general) Schur decomposition reordered such
        that the eigenvalues chosen by the array select are in the top left
        block.
    """
    if b is None:
        eps = np.finfo(a.dtype).eps * tol
        t, z, ev = kla.schur(a)

        # Right-decaying modes.
        select = abs(ev) > 1 + eps
        # Propagating modes.
        propselect = abs(abs(ev) - 1) < eps

        vec_gen = lambda x: kla.evecs_from_schur(t, z, select=x)
        ord_schur = lambda x: kla.order_schur(x, t, z, calc_ev=False)[1]

    else:
        eps = np.finfo(np.common_type(a, b)).eps * tol
        s, t, z, alpha, beta = kla.gen_schur(a, b, calc_q=False)

        # Right-decaying modes.
        select = abs(alpha) > (1 + eps) * abs(beta)
        # Propagating modes.
        propselect = (abs(abs(alpha) - abs(beta)) < eps * abs(beta))

        with np.errstate(divide='ignore', invalid='ignore'):
            ev = alpha / beta
        # Note: the division is OK here, since we later only access
        #       eigenvalues close to the unit circle

        vec_gen = lambda x: kla.evecs_from_gen_schur(s, t, z=z, select=x)
        ord_schur = lambda x: kla.order_gen_schur(x, s, t, z=z,
                                                  calc_ev=False)[2]

    return ev, select, propselect, vec_gen, ord_schur


def make_proper_modes(lmbdainv, psi, extract, tol=1e6):
    """
    Find, normalize and sort the propagating eigenmodes.

    Special care is taken of the case of degenerate k-values, where the
    numerically computed modes are typically a superposition of the real
    modes. In this case, also the proper (orthogonal) modes are computed.
    """
    vel_eps = np.finfo(psi.dtype).eps * tol

    nmodes = psi.shape[1]
    n = len(psi) // 2

    # Array for the velocities.
    velocities = np.empty(nmodes, dtype=float)

    # Calculate the full wave function in real space.
    full_psi = extract(psi, lmbdainv)

    # Find clusters of nearby eigenvalues. Since the eigenvalues occupy the
    # unit circle, special care has to be taken to not introduce a cut at
    # lambda = -1.
    eps = np.finfo(lmbdainv.dtype).eps * tol
    angles = np.angle(lmbdainv)
    sort_order = np.resize(np.argsort(angles), (2 * len(angles,)))
    boundaries = np.argwhere(np.abs(np.diff(lmbdainv[sort_order]))
                             > eps).flatten() + 1

    # Detect the singular case of all eigenvalues equal.
    if boundaries.shape == (0,) and len(angles):
        boundaries = np.array([0, len(angles)])

    for interval in zip(boundaries[:-1], boundaries[1:]):
        if interval[1] > boundaries[0] + len(angles):
            break

        indx = sort_order[interval[0] : interval[1]]

        # If there is a degenerate eigenvalue with several different
        # eigenvectors, the numerical routines return some arbitrary
        # overlap of the real, physical solutions. In order
        # to figure out the correct wave function, we need to
        # have the full, not the projected wave functions
        # (at least to our current knowledge).

        # Finding the true modes is done in two steps:

        # 1. The true transversal modes should be orthogonal to each other, as
        # they share the same Bloch momentum (note that transversal modes with
        # different Bloch momenta k1 and k2 need not be orthogonal, the full
        # modes are orthogonal because of the longitudinal dependence e^{i k1
        # x} and e^{i k2 x}).  The modes with the same k are therefore
        # orthogonalized. Moreover for the velocity to have a proper value the
        # modes should also be normalized.

        q, r = la.qr(full_psi[:, indx], mode='economic')

        # If the eigenvectors were purely real up to this stage,
        # they will typically become complex after the rotation.
        if psi.dtype != np.common_type(psi, r):
            psi = psi.astype(np.common_type(psi, r))
        if full_psi.dtype != np.common_type(full_psi, q):
            full_psi = full_psi.astype(np.common_type(psi, q))

        full_psi[:, indx] = q
        psi[:, indx] = la.solve(r.T, psi[:, indx].T).T

        # 2. Moving infinitesimally away from the degeneracy
        # point, the modes should diagonalize the velocity
        # operator (i.e. when they are non-degenerate any more)
        # The modes are therefore rotated properly such that they
        # diagonalize the velocity operator.
        # Note that step 2. does not give a unique result if there are
        # two modes with the same velocity, or if the modes stay
        # degenerate even for a range of Bloch momenta (and hence
        # must have the same velocity). However, this does not matter,
        # since we are happy with any superposition in this case.

        vel_op = -1j * dot(psi[n:, indx].T.conj(), psi[:n, indx])
        vel_op = vel_op + vel_op.T.conj()
        vel_vals, rot = la.eigh(vel_op)

        # If the eigenvectors were purely real up to this stage,
        # they will typically become complex after the rotation.

        if psi.dtype != np.common_type(psi, rot):
            psi = psi.astype(np.common_type(psi, rot))
        if full_psi.dtype != np.common_type(full_psi, rot):
            full_psi = full_psi.astype(np.common_type(psi, rot))

        psi[:, indx] = dot(psi[:, indx], rot)
        full_psi[:, indx] = dot(full_psi[:, indx], rot)
        velocities[indx] = vel_vals

    if np.any(abs(velocities) < vel_eps):
        raise RuntimeError("Found a mode with zero or close to zero velocity.")
    if 2 * np.sum(velocities < 0) != len(velocities):
        raise RuntimeError("Numbers of left- and right-propagating "
                           "modes differ, possibly due to a numerical "
                           "instability.")
    momenta = -np.angle(lmbdainv)
    order = np.lexsort([velocities, -np.sign(velocities) * momenta,
                        np.sign(velocities)])

    # TODO: Remove the check once we depende on numpy>=1.8.
    if not len(order):
        order = slice(None)
    velocities = velocities[order]
    norm = np.sqrt(abs(velocities))
    full_psi = full_psi[:, order] / norm
    psi = psi[:, order] / norm
    momenta = momenta[order]

    return psi, PropagatingModes(full_psi, velocities, momenta)


def modes(h_cell, h_hop, tol=1e6, stabilization=None):
    """Compute the eigendecomposition of a translation operator of a lead.

    Parameters
    ----------
    h_cell : numpy array, real or complex, shape (N,N) The unit cell
        Hamiltonian of the lead unit cell.
    h_hop : numpy array, real or complex, shape (N,M)
        The hopping matrix from a lead cell to the one on which self-energy
        has to be calculated (and any other hopping in the same direction).
    tol : float
        Numbers and differences are considered zero when they are smaller
        than `tol` times the machine precision.
    stabilization : sequence of 2 booleans or None
        Which steps of the eigenvalue prolem stabilization to perform. If the
        value is `None`, then Kwant chooses the fastest (and least stable)
        algorithm that is expected to be sufficient.  For any other value,
        Kwant forms the eigenvalue problem in the basis of the hopping singular
        values.  The first element set to `True` forces Kwant to add an
        anti-Hermitian term to the cell Hamiltonian before inverting. If it is
        set to `False`, the extra term will only be added if the cell
        Hamiltonian isn't invertible. The second element set to `True` forces
        Kwant to solve a generalized eigenvalue problem, and not to reduce it
        to the regular one.  If it is `False`, reduction to a regular problem
        is performed if possible.  Selecting the stabilization manually is
        mostly necessary for testing purposes.

    Returns
    -------
    propagating : `~kwant.physics.PropagatingModes`
        Contains the array of the wave functions of propagating modes, their
        momenta, and their velocities. It can be used to identify the gauge in
        which the scattering problem is solved.
    stabilized : `~kwant.physics.StabilizedModes`
        A basis of propagating and evanescent modes used by the solvers.

    Notes
    -----
    The propagating modes are sorted according to the longitudinal component of
    their k-vector, with incoming modes having k sorted in descending order,
    and outgoing modes having k sorted in ascending order.  In simple cases
    where bands do not cross, this ordering corresponds to "lowest modes
    first". In general, however, it is necessary to examine the band structure
    -- something this function is not doing by design.

    Propagating modes with the same momentum are orthogonalized. All the
    propagating modes are normalized by current.

    This function uses the most stable and efficient algorithm for calculating
    the mode decomposition that the Kwant authors are aware about. Its details
    are to be published.
    """
    m = h_hop.shape[1]
    n = h_cell.shape[0]

    if (h_cell.shape[0] != h_cell.shape[1] or
        h_cell.shape[0] != h_hop.shape[0]):
        raise ValueError("Incompatible matrix sizes for h_cell and h_hop.")

    if not complex_any(h_hop):
        v = np.zeros((m, 0))
        return (PropagatingModes(np.zeros((n, 0)), np.zeros((0,)),
                                 np.zeros((0,))),
                StabilizedModes(np.zeros((0, 0)), np.zeros((0, 0)), 0, v))

    # Defer most of the calculation to helper routines.
    matrices, v, extract = setup_linsys(h_cell, h_hop, tol, stabilization)
    ev, evanselect, propselect, vec_gen, ord_schur = unified_eigenproblem(
        *(matrices + (tol,)))

    if v is not None:
        n = v.shape[1]

    nrightmovers = np.sum(propselect) // 2
    nevan = n - nrightmovers
    evan_vecs = ord_schur(evanselect)[:, :nevan]

    # Compute the propagating eigenvectors.
    prop_vecs = vec_gen(propselect)
    # Compute their velocity, and, if necessary, rotate them
    prop_vecs, real_space_data = make_proper_modes(
        ev[propselect], prop_vecs, extract, tol)

    vecs = np.c_[prop_vecs[n:], evan_vecs[n:]]
    vecslmbdainv = np.c_[prop_vecs[:n], evan_vecs[:n]]

    return real_space_data, StabilizedModes(vecs, vecslmbdainv, nrightmovers, v)


def selfenergy(h_cell, h_hop, tol=1e6):
    """
    Compute the self-energy generated by the lead.

    Parameters
    ----------
    h_cell : numpy array, real or complex, shape (N,N) The unit cell Hamiltonian
        of the lead unit cell.
    h_hop : numpy array, real or complex, shape (N,M)
        The hopping matrix from a lead cell to the one on which self-energy
        has to be calculated (and any other hopping in the same direction).
    tol : float
        Numbers are considered zero when they are smaller than `tol` times
        the machine precision.

    Returns
    -------
    Sigma : numpy array, real or complex, shape (M,M)
        The computed self-energy. Note that even if `h_cell` and `h_hop` are
        both real, `Sigma` will typically be complex. (More precisely, if there
        is a propagating mode, `Sigma` will definitely be complex.)

    Notes
    -----
    For simplicity this function internally calculates the modes first.
    This may cause a small slowdown, and can be improved if necessary.
    """
    stabilized = modes(h_cell, h_hop, tol)[1]
    return stabilized.selfenergy()


def square_selfenergy(width, hopping, fermi_energy):
    """
    Calculate analytically the self energy for a square lattice.

    The lattice is assumed to have a single orbital per site and
    nearest-neighbor hopping.

    Parameters
    ----------
    width : integer
        width of the lattice
    """

    # Following appendix C of M. Wimmer's diploma thesis:
    # http://www.physik.uni-regensburg.de/forschung/\
    # richter/richter/media/research/publications2004/wimmer-Diplomarbeit.pdf

    # p labels transversal modes.  i and j label the sites of a cell.

    # Precalculate the transverse wave function.
    psi_p_i = np.empty((width, width))
    factor = pi / (width + 1)
    prefactor = sqrt(2 / (width + 1))
    for p in range(width):
        for i in range(width):
            psi_p_i[p, i] = prefactor * sin(factor * (p + 1) * (i + 1))

    # Precalculate the integrals of the longitudinal wave functions.
    def f(q):
        if abs(q) <= 2:
            return q/2 - 1j * sqrt(1 - (q / 2) ** 2)
        else:
            return q/2 - copysign(sqrt((q / 2) ** 2 - 1), q)
    f_p = np.empty((width,), dtype=complex)
    for p in range(width):
        e = 2 * hopping * (1 - cos(factor * (p + 1)))
        q = (fermi_energy - e) / hopping - 2
        f_p[p] = f(q)

    # Put everything together into the self energy and return it.
    result = np.empty((width, width), dtype=complex)
    for i in range(width):
        for j in range(width):
            result[i, j] = hopping * (
                psi_p_i[:, i] * psi_p_i[:, j].conj() * f_p).sum()
    return result
