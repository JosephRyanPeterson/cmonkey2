"""all_tests.py - run all unit tests in the project

This file is part of cMonkey Python. Please see README and LICENSE for
more information and licensing details.
"""
import unittest
from datatypes_test import DataMatrixTest, DataMatrixCollectionTest
from cmonkey_test import CMonkeyTest, MembershipTest
from util_test import DelimitedFileTest, UtilsTest
from util_test import LevenshteinDistanceTest, BestMatchingLinksTest
from organism_test import KeggOrganismCodeMapperTest, RsatOrganismMapperTest
from organism_test import GoTaxonomyMapperTest, OrganismTest

if __name__ == '__main__':
    SUITE = []
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(DataMatrixTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(CMonkeyTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(MembershipTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(OrganismTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(
            DataMatrixCollectionTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(
            DelimitedFileTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(
            KeggOrganismCodeMapperTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(
            GoTaxonomyMapperTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(
            RsatOrganismMapperTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(
            LevenshteinDistanceTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(
            BestMatchingLinksTest))
    SUITE.append(unittest.TestLoader().loadTestsFromTestCase(
            UtilsTest))
    unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(SUITE))
