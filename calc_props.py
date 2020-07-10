###############################
import argparse
import os
import sys
import csv
from functools import partial
import openbabel as ob
import pybel
import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem
from tqdm import trange
from multiprocessing.context import TimeoutError
from multiprocessing import Pool

###############################

__doc__ = """Performs calculation of physiochemical properties of potential antibiotics. SMILES strings are parsed,
conformers are generated, and properties calculated. Properties include: chemical formula, molecular weight, rotatable
bonds, globularity, and PBF.
"""


PRIMARY_AMINE_SMARTS = pybel.Smarts('[$([N;H2;X3][CX4]),$([N;H3;X4+][CX4])]')


def main():
    args = parse_args(sys.argv[1:])
    if args.smiles:
        properties = average_properties(args.smiles)
        # A file will be written if command line option provide, otherwise write to stdout
        if args.output:
            mols_to_write = [properties]
            write_csv(mols_to_write, args.output)
        else:
            report_properties(properties)
    elif args.batch_file:
        with open(args.batch_file) as f:
            reader = csv.DictReader(f)
            read_fieldnames = list(reader.fieldnames)
            data = list(reader)

        write_fieldnames = read_fieldnames + ['primary_amine', 'globularity', 'rotatable_bonds']

        with Pool() as pool, open(args.output, 'w') as f:
            writer = csv.DictWriter(f, fieldnames=write_fieldnames)
            writer.writeheader()
            iterator = pool.imap_unordered(partial(average_properties_safe, smiles_column=args.smiles_column), data)

            for _ in trange(len(data)):
                try:
                    row = iterator.next(timeout=args.timeout)

                    if row is not None:
                        writer.writerow(row)
                except TimeoutError:
                    pass


def parse_args(arguments):
    """Parse the command line options.
    :return:  All script options
    """
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("-s", "--smiles", dest="smiles", metavar="SMILES string", default=None)
    group.add_argument("-b", "--batch", dest="batch_file", metavar="Batch file", default=None)
    group.add_argument("-c", "--column", dest="smiles_column", metavar="Smiles column", default='canonical_smiles')
    parser.add_argument("-o", "--output", dest="output", metavar="Output file", default=None,
                        help="Defaults to csv file with same name as input")
    parser.add_argument("-t", "--timeout", dest="timeout", type=int, metavar="Timeout", default=10)

    args = parser.parse_args(arguments)
    if not args.smiles and not args.batch_file:
        parser.error("Input structure is needed")

    # If no output file is specified in batch mode, then replace the file extension of the input with .csv
    if args.batch_file and not args.output:
        args.output = os.path.splitext(args.batch_file)[0] + '.csv'

    return args


def report_properties(properties):
    """
    Write out the results of physiochemical properties to stdout

    :param smiles: SMILES string of input molecule
    :param properties: physiochemical properties to report
    :type smiles: str
    :type properties: dict
    :return: None
    """
    print("Properties for %s" % properties['smiles'])
    print("--------------------------")
    print("Mol. Wt.:\t%f" % properties['molwt'])
    print("Formula:\t%s" % properties['formula'])
    print("RB:\t\t%i" % properties['rb'])
    print("Glob:\t\t%f" % properties['glob'])
    print("PBF:\t\t%f" % properties['pbf'])


def parse_batch(filename):
    """
    Read a file containing names and SMILES strings

    Expects a file with no header in which each line contains a SMILES string followed by a name for the molecule.
    SMILES and name can be separated by any whitespace.

    :param filename: file to read
    :type filename: str
    :return: List of tuples with names and SMILES
    :rtype: list
    """
    smiles = []
    names = []
    with(open(filename, 'r')) as batch:
        for line in batch:
            (smi, name) = tuple(line.split())
            smiles.append(smi)
            names.append(name)
    return zip(smiles, names)


def write_csv(mols_to_write, filename):
    """
    Write out results of physiochemical properties

    :param mols_to_write: list of molecule properties to write
    :param filename: path to file to write
    :type mols_to_write: list
    :type filename: str
    :return: None
    """
    with(open(filename, 'w')) as out:
        # fieldnames = ['smiles', 'formula', 'molwt', 'rb', 'glob', 'pbf']
        fieldnames = ['smiles', 'formula', 'molwt', 'rb', 'glob', 'primary_amine']
        writer = csv.DictWriter(out, fieldnames=fieldnames)
        writer.writeheader()
        for mol in mols_to_write:
            writer.writerow(mol)


def average_properties_safe(row, smiles_column='smiles'):
    try:
        properties = average_properties(row[smiles_column])
        row.update(properties)
        return row
    except Exception as e:
        print(e)
        return None


def average_properties(smiles):
    """
    Calculate all relevant properties for a given molecule averaged across conformers

    :param mol: input molecule smiles
    :type mol: openbabel.OBMol
    :return: dictionary of properties
    :rtype dict

    ..todo: remove reliance on pybel
    """
    mol = smiles_to_ob(smiles)
    mols = run_confab(mol)
    num_confs = mols.NumConformers()

    globs = np.empty(num_confs)
    # pbfs = np.empty(num_confs)
    for i in range(num_confs):
        mols.SetConformer(i)
        pymol = pybel.Molecule(mols)
        # calculate properties
        globs[i] = calc_glob(pymol)
        # pbfs[i] = calc_pbf(pymol)

    data = {
        'rotatable_bonds': rotatable_bonds(pymol),
        'globularity': np.mean(globs),
        'primary_amine': has_primary_amine(pymol),
        # 'pbf': np.mean(pbfs)
    }
    return data


def smiles_to_ob(mol_string):
    """
    Reads a SMILES string and creates a molecule object

    Currently, an initial guess at 3D geometry is performed by RDkit.

    :param mol_string: SMILES string
    :type mol_string: str
    :return: molecule object
    :rtype: openbabel.OBMol
    """
    mol = initial_geom_guess(mol_string)
    obmol = ob.OBMol()
    obConv = ob.OBConversion()
    obConv.SetInAndOutFormats("mol", "mol")
    obConv.ReadString(obmol, mol)
    return obmol


def initial_geom_guess(smiles):
    """
    Parses a SMILES string and performs an initial guess of geometry

    :param smiles: SMILES structure string
    :return: String with Mol structure text
    :rtype: str

    ..todo: use openbabel for initial guess
    """
    m = Chem.MolFromSmiles(smiles)
    m2 = Chem.AddHs(m)

    # Generate initial guess
    AllChem.EmbedMolecule(m2, AllChem.ETKDG())
    AllChem.MMFFOptimizeMolecule(m2)

    # Write mol file
    return Chem.MolToMolBlock(m2)


def run_confab(mol, rmsd_cutoff=0.5, conf_cutoff=100000, energy_cutoff=50.0, confab_verbose=False):
    """
    Generate ensemble of conformers to perform calculations on

    :param mol: initial molecule to generate conformers from
    :param rmsd_cutoff: similarity threshold for conformers, default: 0.5
    :param conf_cutoff: max number of conformers to generate, default: 100,000
    :param energy_cutoff: max relative energy between conformers, default: 50
    :param confab_verbose: whether confab should report on rotors
    :type mol: openbabel.OBMol
    :type rmsd_cutoff: float
    :type conf_cutoff: int
    :type energy_cutoff: float
    :type confab_verbose: bool
    :return: list of conformers for a given molecule
    :rtype: openbabel.OBMol
    """
    pff = ob.OBForceField_FindType("mmff94")
    pff.Setup(mol)

    pff.DiverseConfGen(rmsd_cutoff, conf_cutoff, energy_cutoff, confab_verbose)

    pff.GetConformers(mol)

    return mol


def calc_glob(mol):
    """
    Calculates the globularity (glob) of a molecule

    glob varies from 0 to 1 with completely flat molecules like benzene having a
    glob of 0 and spherical molecules like adamantane having a glob of 1

    :param mol: pybel molecule object
    :type mol: pybel.Molecule
    :return: globularity of molecule
    :rtype: float | int
    """
    points = get_atom_coords(mol, heavy_only=False)
    if points is None:
        return 0
    points = points.T

    # calculate covariance matrix
    cov_mat = np.cov([points[0, :], points[1, :], points[2, :]])

    # calculate eigenvalues of covariance matrix and sort
    vals, vecs = np.linalg.eig(cov_mat)
    vals = np.sort(vals)[::-1]

    # glob is ratio of last eigenvalue and first eigenvalue
    if vals[0] != 0:
        return vals[-1] / vals[0]
    else:
        return -1


def calc_pbf(mol):
    """
    Uses SVD to fit atoms in molecule to a plane then calculates the average
    distance to that plane.

    :param mol: pybel molecule object
    :type mol: pybel.Molecule
    :return: average distance of all atoms to the best fit plane
    :rtype: float
    """
    points = get_atom_coords(mol)
    c, n = svd_fit(points)
    pbf = calc_avg_dist(points, c, n)
    return pbf


def has_primary_amine(mol):
    """
    Uses SMARTS to determine if the molecule has a primary amine.

    :param mol: pybel molecule object
    :return: 1 if mol has a primary amine, 0 otherwise
    :rtype: int
    """
    primary_amines = PRIMARY_AMINE_SMARTS.findall(mol)

    return int(len(primary_amines) > 0)


def rotatable_bonds(mol):
    """
    Calculates the number of rotatable bonds in a molecules. Rotors are defined
    as any non-terminal bond between heavy atoms, excluding amides

    :param mol: pybel molecule object
    :type mol: pybel.Molecule
    :return rb: number of rotatable bonds
    :rtype int
    """
    rb = 0
    for bond in ob.OBMolBondIter(mol.OBMol):
        if is_rotor(bond):
            rb += 1
    return rb


def is_rotor(bond, include_amides=False):
    """
    Determines if a bond is rotatable

    Rules for rotatable bonds:
    Must be a single or triple bond
    Must include two heavy atoms
    Cannot be terminal
    Cannot be in a ring
    If a single bond to one sp hybridized atom, not rotatable

    :param bond:
    :return: If a bond is rotatable
    :rtype: bool
    """
    # Must be single or triple bond
    if bond.IsDouble(): return False

    # Don't count the N-C bond of amides
    if bond.IsAmide() and not include_amides: return False

    # Not in a ring
    if bond.FindSmallestRing() is not None: return False

    # Don't count single bonds adjacent to triple bonds, still want to count the triple bond
    if (bond.GetBeginAtom().GetHyb() == 1) != (bond.GetEndAtom().GetHyb() == 1): return False

    # Cannot be terminal
    if bond.GetBeginAtom().GetHvyValence() > 1 and bond.GetEndAtom().GetHvyValence() > 1: return True


def calc_avg_dist(points, C, N):
    """
    Calculates the average distance a given set of points is from a plane

    :param points: numpy array of points
    :param C: centroid vector of plane
    :param N: normal vector of plane
    :return Average distance of each atom from the best-fit plane
    """
    sum = 0
    for xyz in points:
        sum += abs(distance(xyz, C, N))
    return sum / len(points)


def get_atom_coords(mol, heavy_only=False):
    """
    Retrieve the 3D coordinates of all atoms in a molecules

    :param mol: pybel molecule object
    :return numpy array of coordinates
    """
    num_atoms = len(mol.atoms)
    pts = np.empty(shape=(num_atoms, 3))
    atoms = mol.atoms

    for a in range(num_atoms):
        pts[a] = atoms[a].coords

    return pts


def svd_fit(X):
    """
    Fitting algorithmn was obtained from https://gist.github.com/lambdalisue/7201028
    Find (n - 1) dimensional standard (e.g. line in 2 dimension, plane in 3
    dimension, hyperplane in n dimension) via solving Singular Value
    Decomposition.
    The idea was explained in the following references
    - http://www.caves.org/section/commelect/DUSI/openmag/pdf/SphereFitting.pdf
    - http://www.geometrictools.com/Documentation/LeastSquaresFitting.pdf
    - http://www.ime.unicamp.br/~marianar/MI602/material%20extra/svd-regression-analysis.pdf

    :Example:
        >>> XY = [[0, 1], [3, 3]]
        >>> XY = np.array(XY)
        >>> C, N = svd_fit(XY)
        >>> C
        array([ 1.5,  2. ])
        >>> N
        array([-0.5547002 ,  0.83205029])

    :param X:n x m dimensional matrix which n indicate the number of the dimension and m indicate the number of points
    :return [C, N] where C is a centroid vector and N is a normal vector
    :rtype tuple
    """
    # Find the average of points (centroid) along the columns
    C = np.average(X, axis=0)
    # Create CX vector (centroid to point) matrix
    CX = X - C
    # Singular value decomposition
    U, S, V = np.linalg.svd(CX)
    # The last row of V matrix indicate the eigenvectors of
    # smallest eigenvalues (singular values).
    N = V[-1]
    return C, N


def distance(x, C, N):
    """
    Calculate an orthogonal distance between the points and the standard
    Args:
    :param x: n x m dimensional matrix
    :param C: n dimensional vector whicn indicate the centroid of the standard
    :param N: n dimensional vector which indicate the normal vector of the standard
    :return m dimensional vector which indicate the orthogonal distance. the value
            will be negative if the points beside opposite side of the normal vector
    """
    return np.dot(x - C, N)


if __name__ == '__main__':
    main()
