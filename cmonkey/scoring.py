"""scoring.py - cMonkey scoring base classes

This file is part of cMonkey Python. Please see README and LICENSE for
more information and licensing details.
"""
LOG_FORMAT = '%(asctime)s %(levelname)-8s %(message)s'

import logging
import os
import datamatrix as dm
from datetime import date
import util
import membership as memb
import numpy as np

# Official keys to access values in the configuration map
KEY_ORGANISM_CODE = 'organism_code'
KEY_NUM_ITERATIONS = 'num_iterations'
KEY_MATRIX_FILENAMES = 'matrix_filenames'
KEY_CACHE_DIR = 'cache_dir'
KEY_SEQUENCE_TYPES = 'sequence_types'
KEY_SEARCH_DISTANCES = 'search_distances'
KEY_SCAN_DISTANCES = 'scan_distances'
KEY_MULTIPROCESSING = 'multiprocessing'
KEY_RUN_AT_ITERATIONS = 'run_at_iterations'

KEY_MOTIF_MIN_CLUSTER_ROWS_ALLOWED = 'motif.min_cluster_rows_allowed'
KEY_MOTIF_MAX_CLUSTER_ROWS_ALLOWED = 'motif.max_cluster_rows_allowed'
MOTIF_MIN_CLUSTER_ROWS_ALLOWED = 3
MOTIF_MAX_CLUSTER_ROWS_ALLOWED = 70
USE_MULTIPROCESSING = True


class ScoringFunctionBase:
    """Base class for scoring functions"""

    def __init__(self, membership, matrix, weight_func,
                 config_params):
        """creates a function instance"""
        self.__membership = membership
        self.__matrix = matrix
        self.__weight_func = weight_func
        self.config_params = config_params
        if config_params == None:
            raise Exception('NO CONFIG PARAMS !!!')

    def name(self):
        """returns the name of this function"""
        raise Exception("please implement me")

    def membership(self):
        """returns this function's membership object"""
        return self.__membership

    def matrix(self):
        """returns this function's matrix object"""
        return self.__matrix

    def compute(self, iteration, reference_matrix=None):
        """general compute method, iteration is the 0-based iteration number
        the reference_matrix is actually a hack that allows the scoring
        function to normalize its scores to the range of a reference
        score matrix. In the normal case, those would be the gene expression
        row scores"""
        raise Exception("please implement me")

    def num_clusters(self):
        """returns the number of clusters"""
        return self.__membership.num_clusters()

    def gene_names(self):
        """returns the gene names"""
        return self.__matrix.row_names()

    def num_genes(self):
        """returns the number of rows"""
        return self.__matrix.num_rows()

    def gene_at(self, index):
        """returns the gene at the specified index"""
        return self.__matrix.row_name(index)

    def rows_for_cluster(self, cluster):
        """returns the rows for the specified cluster"""
        return self.__membership.rows_for_cluster(cluster)

    def weight(self, iteration):
        """returns the weight for the specified iteration"""
        return self.__weight_func(iteration)

    def store_checkpoint_data(self, shelf):
        """Default implementation does not store checkpoint data"""
        pass

    def restore_checkpoint_data(self, shelf):
        """Default implementation does not store checkpoint data"""
        pass

    def run_this_iteration(self, iteration):
        if self.config_params.has_key['runiters']:
            if iteration in self.config_params['runiters']: return True
        return False

class ColumnScoringFunction(ScoringFunctionBase):
    """Scoring algorithm for microarray data based on conditions.
    Note that the score does not correspond to the normal scoring
    function output format and can therefore not be combined in
    a generic way (the format is |condition x cluster|)"""

    def __init__(self, membership, matrix,
                 config_params=None):
        """create scoring function instance"""
        ScoringFunctionBase.__init__(self, membership,
                                     matrix, None, config_params)

    def name(self):
        """returns the name of this scoring function"""
        return "Column"

    def compute(self, iteration, ref_matrix=None):
        """compute method, iteration is the 0-based iteration number"""
        start_time = util.current_millis()
        result = compute_column_scores(self.membership(),
                                       self.matrix(),
                                       self.num_clusters())
        elapsed = util.current_millis() - start_time
        logging.info("COLUMN SCORING TIME: %f s.", (elapsed / 1000.0))
        return result

    def apply_weight(self, result, iteration):
        """applies the stored weight"""
        return result

def compute_column_scores(membership, matrix, num_clusters):
    """Computes the column scores for the specified number of clusters"""

    def compute_substitution(cluster_column_scores):
        """calculate substitution value for missing column scores"""
        membership_values = []
        for cluster in xrange(1, num_clusters + 1):
            columns = membership.columns_for_cluster(cluster)
            column_scores = cluster_column_scores[cluster - 1]
            if column_scores != None:
                for row in xrange(column_scores.num_rows()):
                    for col in xrange(column_scores.num_columns()):
                        if column_scores.column_name(col) in columns:
                            membership_values.append(column_scores[row][col])
        return util.quantile(membership_values, 0.95)

    cluster_column_scores = []
    null_scores_found = False
    for cluster in xrange(1, num_clusters + 1):
        submatrix = matrix.submatrix_by_name(
            row_names=membership.rows_for_cluster(cluster))
        if submatrix.num_rows() > 1:
            cluster_column_scores.append(compute_column_scores_submatrix(
                    submatrix))
        else:
            cluster_column_scores.append(None)
            null_scores_found = True

    if null_scores_found:
        substitution = compute_substitution(cluster_column_scores)

    # Convert scores into a matrix that have the clusters as columns
    # and conditions in the rows
    result = dm.DataMatrix(matrix.num_columns(), num_clusters,
                           row_names=matrix.column_names())
    for cluster in xrange(num_clusters):
        column_scores = cluster_column_scores[cluster]
        for row_index in xrange(matrix.num_columns()):
            if column_scores == None:
                result[row_index][cluster] = substitution
            else:
                result[row_index][cluster] = column_scores[0][row_index]
    return result

def compute_column_scores_submatrix(matrix):
    """For a given matrix, compute the column scores.
    This is used to compute the column scores of the sub matrices that
    were determined by the pre-seeding, so typically, matrix is a
    submatrix of the input matrix that contains only the rows that
    belong to a certain cluster.
    The result is a DataMatrix with one row containing all the
    column scores

    This function normalizes diff^2 by the mean expression level, similar
    to "Index of Dispersion", see
    http://en.wikipedia.org/wiki/Index_of_dispersion
    for details
    """
    colmeans = matrix.column_means()
    matrix_minus_colmeans_squared = subtract_and_square(matrix, colmeans)
    var_norm = np.abs(colmeans) + 0.01
    result = util.column_means(matrix_minus_colmeans_squared) / var_norm
    return dm.DataMatrix(1, matrix.num_columns(), ['Col. Scores'],
                         matrix.column_names(), [result])


def subtract_and_square(matrix, vector):
    """reusable function to subtract a vector from each row of
    the input matrix and square the values in the result matrix"""
    return np.square(matrix.values() - vector)


class ScoringFunctionCombiner:
    """Taking advantage of the composite pattern, this combiner function
    exposes the basic interface of a scoring function in order to
    allow for nested scoring functions as they are used in the motif
    scoring
    """
    def __init__(self, membership, scoring_functions, weight_func=None,
                 log_subresults=False):
        """creates a combiner instance"""
        self.__membership = membership
        self.__scoring_functions = scoring_functions
        self.__log_subresults = log_subresults
        self.__weight_func = weight_func

    def compute(self, iteration, ref_matrix=None):
        """compute scores for one iteration"""
        result_matrices = []
        score_weights = []
        reference_matrix = ref_matrix
        for scoring_function in self.__scoring_functions:
            # This  is actually a hack in order to propagate
            # a reference matrix to the compute function
            # This could have negative impact on scalability
            if reference_matrix == None and len(result_matrices) > 0:
                reference_matrix = result_matrices[0]

            matrix = scoring_function.compute(iteration, reference_matrix)
            if matrix != None:
                result_matrices.append(matrix)
                score_weights.append(scoring_function.weight(iteration))

                if self.__log_subresults:
                    self.__log_subresult(scoring_function, matrix)

        if len(result_matrices) > 1:
            logging.info(
                "COMBINING THE SCORES OF %d matrices (quantile normalize)",
                len(result_matrices))
            start_time = util.current_millis()
            result_matrices = dm.quantile_normalize_scores(result_matrices,
                                                           score_weights)
            elapsed = util.current_millis() - start_time
            logging.info("SCORES COMBINED IN %f s", elapsed / 1000.0)

        if len(result_matrices) == 0:
            logging.warn("NO RESULTS !!!")
        combined_score = (result_matrices[0] *
                          self.__scoring_functions[0].weight(iteration))
        for index in xrange(1, len(result_matrices)):
            combined_score += (
                result_matrices[index] *
                self.__scoring_functions[index].weight(iteration))
        return combined_score

    def __log_subresult(self, score_function, matrix):
        """output an accumulated subresult to the log"""
        scores = []
        for cluster in xrange(1, matrix.num_columns() + 1):
            cluster_rows = self.__membership.rows_for_cluster(cluster)
            for row in xrange(matrix.num_rows()):
                if matrix.row_name(row) in cluster_rows:
                    scores.append(matrix[row][cluster - 1])
        logging.info("function '%s', trim mean score: %f",
                     score_function.name(),
                     util.trim_mean(scores, 0.05))

    def weight(self, iteration):
        """returns the weight for the specified iteration"""
        return self.__weight_func(iteration)

    def store_checkpoint_data(self, shelf):
        """recursively invokes store_checkpoint_data() on the children"""
        for scoring_func in self.__scoring_functions:
            scoring_func.store_checkpoint_data(shelf)

    def restore_checkpoint_data(self, shelf):
        """recursively invokes store_checkpoint_data() on the children"""
        for scoring_func in self.__scoring_functions:
            scoring_func.restore_checkpoint_data(shelf)


class ConfigurationBase:
    """configuration base class"""

    def __init__(self, config_params, checkpoint_file=None):
        """create instance"""
        logging.basicConfig(format=LOG_FORMAT,
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=logging.DEBUG)

        self.__start_iteration = 0
        self.__matrix = None
        self.__membership = None
        self.__organism = None
        self.__row_scoring = None
        self.__column_scoring = None

        if checkpoint_file == None:
            self.config_params = config_params
            if not os.path.exists(config_params[KEY_CACHE_DIR]):
                os.mkdir(config_params[KEY_CACHE_DIR])
            today = date.today()
            self.__checkpoint_basename = "cmonkey-checkpoint-%s-%d%d%d" % (
                config_params[KEY_ORGANISM_CODE], today.year,
                today.month, today.day)
        else:
            self.__checkpoint_basename = checkpoint_file.split(".")[0]
            self.init_from_checkpoint(checkpoint_file)
        logging.info("Checkpoints will be saved to '%s'",
                     self.__checkpoint_basename)

    def organism_code(self):
        """returns the organism code"""
        return self.config_params[KEY_ORGANISM_CODE]

    def start_iteration(self):
        """returns the start iteration, if restored from a check point,
        this is the iteration after the save point"""
        return self.__start_iteration

    def num_iterations(self):
        """returns the number of iterations"""
        return self.config_params[KEY_NUM_ITERATIONS]

    def cache_dir(self):
        """returns the cache directory"""
        return self.config_params[KEY_CACHE_DIR]

    def matrix(self):
        """returns the input matrix"""
        if self.__matrix == None:
            self.__matrix = (self.read_matrix(
                self.config_params[KEY_MATRIX_FILENAMES][0]).
                             sorted_by_row_name())
            logging.info("READ MATRIX, # GENES: %d, # CONDITIONS: %d",
                         self.__matrix.num_rows(),
                         self.__matrix.num_columns())
        return self.__matrix

    def membership(self):
        """returns the seeded membership"""
        if self.__membership == None:
            logging.info("creating and seeding memberships")
            self.__membership = self.make_membership()
        return self.__membership

    def organism(self):
        """returns the organism object to work on"""
        if self.__organism == None:
            self.__organism = self.make_organism()
        return self.__organism

    def row_scoring(self):
        """returns the row scoring function"""
        if self.__row_scoring == None:
            self.__row_scoring = self.make_row_scoring()
        return self.__row_scoring

    def column_scoring(self):
        """returns the column scoring function"""
        if self.__column_scoring == None:
            self.__column_scoring = ColumnScoringFunction(
                self.membership(), self.matrix(),
                config_params=self.config_params)
        return self.__column_scoring

    def make_membership(self):
        """implement in derived class"""
        pass

    def read_matrix(self, filename):
        """implement in derived class"""
        pass

    def make_organism(self):
        """implement in derived class"""
        pass

    def make_row_scoring(self):
        """implement in derived class"""
        pass

    def save_checkpoint_data(self, iteration):
        """save checkpoint data for the specified iteration"""
        with util.open_shelf("%s.%d" % (self.__checkpoint_basename,
                                        iteration)) as shelf:
            shelf['config'] = self.config_params
            shelf['iteration'] = iteration
            self.membership().store_checkpoint_data(shelf)
            self.row_scoring().store_checkpoint_data(shelf)
            self.column_scoring().store_checkpoint_data(shelf)

    def init_from_checkpoint(self, checkpoint_filename):
        """initialize this object from a checkpoint file"""
        logging.info("Continue run using checkpoint file '%s'",
                     checkpoint_filename)
        with util.open_shelf(checkpoint_filename) as shelf:
            self.config_params = shelf['config']
            self.__start_iteration = shelf['iteration'] + 1

            self.__membership = memb.ClusterMembership.restore_from_checkpoint(
                self.config_params, shelf)
            self.row_scoring().restore_checkpoint_data(shelf)
            self.column_scoring().restore_checkpoint_data(shelf)


class ConfigurationBuilder:
    """A helper class to define a configuration dictionary"""

    def __init__(self):
        """creates the instance"""
        # initialize with defaults
        self.params = {
            memb.KEY_CLUSTERS_PER_ROW: memb.CLUSTERS_PER_ROW,
            memb.KEY_CLUSTERS_PER_COL: memb.CLUSTERS_PER_COL,
            memb.KEY_NUM_CLUSTERS: memb.NUM_CLUSTERS,
            memb.KEY_PROB_ROW_CHANGE: memb.PROB_SEEING_ROW_CHANGE,
            memb.KEY_PROB_COL_CHANGE: memb.PROB_SEEING_COL_CHANGE,
            memb.KEY_MAX_CHANGES_PER_ROW: memb.MAX_CHANGES_PER_ROW,
            memb.KEY_MAX_CHANGES_PER_COL: memb.MAX_CHANGES_PER_COL,
            memb.KEY_MIN_CLUSTER_ROWS_ALLOWED: memb.MIN_CLUSTER_ROWS_ALLOWED,
            KEY_MOTIF_MIN_CLUSTER_ROWS_ALLOWED: MOTIF_MIN_CLUSTER_ROWS_ALLOWED,
            KEY_MOTIF_MAX_CLUSTER_ROWS_ALLOWED: MOTIF_MAX_CLUSTER_ROWS_ALLOWED,
            KEY_MULTIPROCESSING: USE_MULTIPROCESSING
            }

    def build(self):
        """returns the object that was built"""
        return self.params

    def with_organism(self, organism_code):
        """define the organism code"""
        self.params[KEY_ORGANISM_CODE] = organism_code
        return self

    def with_num_iterations(self, num_iterations):
        """define number of iterations"""
        self.params[KEY_NUM_ITERATIONS] = num_iterations
        return self

    def with_matrix_filenames(self, filenames):
        """define the input matrix filenames. currently, only
        the first one will be used"""
        self.params[KEY_MATRIX_FILENAMES] = filenames
        return self

    def with_cache_dir(self, cachedir):
        """define the cache directory"""
        self.params[KEY_CACHE_DIR] = cachedir
        return self

    def with_num_clusters(self, num_clusters):
        """define the number of clusters"""
        self.params[memb.KEY_NUM_CLUSTERS] = num_clusters
        return self

    def with_sequence_types(self, sequence_types):
        """define the sequence types"""
        self.params[KEY_SEQUENCE_TYPES] = sequence_types
        return self

    def with_search_distances(self, distances):
        """define the search distances"""
        self.params[KEY_SEARCH_DISTANCES] = distances
        return self

    def with_scan_distances(self, distances):
        """define the scan instances"""
        self.params[KEY_SCAN_DISTANCES] = distances
        return self

    def with_multiprocessing(self, flag):
        """define whether to use multiprocessing"""
        self.params[KEY_MULTIPROCESSING] = flag
        return self

    def with_max_cluster_rows(self, num_rows):
        """define whether to use multiprocessing"""
        self.params[KEY_MOTIF_MAX_CLUSTER_ROWS_ALLOWED] = num_rows
        return self

    def with_run_at_iterations(self, iterations):
        """define which iterations during which to run"""
        self.params[KEY_RUN_AT_ITERATIONS] = iterations
        return self
