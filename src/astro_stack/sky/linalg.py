"""Exact rational linear algebra for the spatial background model.

Every quantity is a :class:`fractions.Fraction`; rank, linear support,
estimability and minimum-variance decisions never use floating point. The
matrices here are tiny (at most a few hundred rows/columns) so readability is
preferred over sparse tricks.
"""
from fractions import Fraction


def rref(matrix):
    """Reduced row echelon form of a copy of ``matrix`` over the rationals.

    Returns ``(reduced, pivot_columns)``.
    """
    reduced = [list(row) for row in matrix]
    if not reduced:
        return [], []
    rows = len(reduced)
    columns = len(reduced[0])
    pivots = []
    rank = 0
    for column in range(columns):
        candidate = next((row for row in range(rank, rows)
                          if reduced[row][column] != 0), None)
        if candidate is None:
            continue
        reduced[rank], reduced[candidate] = reduced[candidate], reduced[rank]
        pivot_value = reduced[rank][column]
        reduced[rank] = [value / pivot_value for value in reduced[rank]]
        for row in range(rows):
            if row != rank and reduced[row][column] != 0:
                factor = reduced[row][column]
                reduced[row] = [
                    value - factor * reduced[rank][j]
                    for j, value in enumerate(reduced[row])]
        pivots.append(column)
        rank += 1
        if rank == rows:
            break
    return reduced, pivots


def rank_of(matrix):
    return len(rref(matrix)[1])


def is_consistent(augmented):
    """An augmented matrix [A|b] is consistent iff no row reads [0...0|c!=0]."""
    reduced, pivots = rref(augmented)
    columns = len(augmented[0])
    return all(pivot < columns - 1 for pivot in pivots)


def row_space_basis(matrix):
    """Nonzero RREF rows of ``matrix``: a canonical basis of its row space."""
    reduced, _pivots = rref(matrix)
    return [row for row in reduced if any(value != 0 for value in row)]


def nullspace(matrix):
    """A basis (list of column vectors) of the right nullspace of ``matrix``."""
    reduced, pivots = rref(matrix)
    columns = len(matrix[0])
    free = [column for column in range(columns) if column not in pivots]
    basis = []
    pivot_of_row = {row: column for row, column in enumerate(pivots)}
    for free_column in free:
        vector = [Fraction(0) for _ in range(columns)]
        vector[free_column] = Fraction(1)
        for row, pivot_column in pivot_of_row.items():
            vector[pivot_column] = -reduced[row][free_column]
        basis.append(vector)
    return basis


def solve_particular(augmented):
    """One exact solution x of A x = b; raises if the system is inconsistent.

    Free variables are set to zero. Callers that need every solution should
    combine the result with :func:`nullspace`.
    """
    reduced, pivots = rref(augmented)
    columns = len(augmented[0]) - 1
    if any(pivot == columns for pivot in pivots):
        raise ValueError("linear system is inconsistent")
    solution = [Fraction(0) for _ in range(columns)]
    for row, pivot in enumerate(pivots):
        solution[pivot] = reduced[row][columns]
    return solution


def mat_vec(matrix, vector):
    return [sum((matrix[i][j] * vector[j] for j in range(len(vector))),
                Fraction(0)) for i in range(len(matrix))]


def transpose(matrix):
    return [list(row) for row in zip(*matrix)] if matrix else []


def mat_mul(left, right):
    return [[sum((left[i][k] * right[k][j]
                  for k in range(len(right))), Fraction(0))
             for j in range(len(right[0]))] for i in range(len(left))]
