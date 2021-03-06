# vi: sw=4 ts=4 et:
import os
import shutil
from datetime import date, datetime
import json
import numpy as np
import gc
import re
import logging
import gzip
import sqlite3
from decimal import Decimal
import bz2
from pkg_resources import Requirement, resource_filename, DistributionNotFound

import cmonkey.config as config
import cmonkey.microarray as microarray
import cmonkey.membership as memb
import cmonkey.meme as meme
import cmonkey.motif as motif
import cmonkey.util as util
import cmonkey.rsat as rsat
import cmonkey.microbes_online as microbes_online
import cmonkey.organism as org
import cmonkey.scoring as scoring
import cmonkey.network as nw
import cmonkey.stringdb as stringdb
import cmonkey.debug as debug
import cmonkey.sizes as sizes
import cmonkey.thesaurus as thesaurus
import cmonkey.BSCM as BSCM

# Python2/Python3 compatibility
try:
    import cPickle as pickle
except ImportError:
    import pickle

try:
    xrange
except NameError:
    xrange = range


USER_KEGG_FILE_PATH = 'cmonkey/default_config/KEGG_taxonomy'
USER_GO_FILE_PATH = 'cmonkey/default_config/proteome2taxid'

# pipeline paths
PIPELINE_USER_PATHS = {
    'default': 'cmonkey/default_config/default_pipeline.json',
    'rows': 'cmonkey/default_config/rows_pipeline.json',
    'rowsandmotifs': 'cmonkey/default_config/rows_and_motifs_pipeline.json',
    'rowsandnetworks': 'cmonkey/default_config/rows_and_networks_pipeline.json'
}


COG_WHOG_URL = 'ftp://ftp.ncbi.nih.gov/pub/COG/COG/whog'
STRING_URL_PATTERN = "http://networks.systemsbiology.net/string9/%s.gz"

# We support non-microbes the easy way for now, until we have a working
# database scheme
VERTEBRATES = {'hsa', 'mmu', 'rno'}

class CMonkeyRun:
    def __init__(self, ratios, args_in):
        self.__membership = None
        self.__organism = None
        self.config_params = args_in
        self.ratios = ratios
        if args_in['resume']:
            self.row_seeder = memb.make_db_row_seeder(args_in['out_database'])

            if args_in['new_data_file']:  # data file has changed
                self.column_seeder = microarray.seed_column_members
            else:
                self.column_seeder = memb.make_db_column_seeder(args_in['out_database'])
        else:
            self.row_seeder = memb.make_kmeans_row_seeder(args_in['num_clusters'])
            self.column_seeder = microarray.seed_column_members
        self.__conn = None

        today = date.today()
        logging.info('Input matrix has # rows: %d, # columns: %d',
                     ratios.num_rows, ratios.num_columns)
        logging.info("# clusters/row: %d", args_in['memb.clusters_per_row'])
        logging.info("# clusters/column: %d", args_in['memb.clusters_per_col'])
        logging.info("# CLUSTERS: %d", args_in['num_clusters'])
        logging.info("use operons: %d", args_in['use_operons'])

        if args_in['MEME']['version']:
            logging.info('using MEME version %s', args_in['MEME']['version'])
        else:
            logging.error('MEME not detected - please check')

    def cleanup(self):
        """cleanup this run object"""
        if self.__conn is not None:
            self.__conn.close()
            self.__conn = None

    def __dbconn(self):
        """Returns an autocommit database connection. We maintain a single database
        connection throughout the life of this run objec"""
        if self.__conn is None:
            self.__conn = sqlite3.connect(self['out_database'], 15, isolation_level='DEFERRED')
        return self.__conn

    def __create_output_database(self):
        conn = self.__dbconn()
        # these are the tables for storing cmonkey run information.
        # run information
        conn.execute('''create table run_infos (start_time timestamp,
                        finish_time timestamp,
                        num_iterations int, last_iteration int,
                        organism text, species text,
                        ncbi_code int,
                        num_rows int,
                        num_columns int, num_clusters int, git_sha text)''')

        # stats tables
        # Note: there is some redundancy with the result tables here.
        # ----- I measured the cost for creating those on the fly and
        #       it is more expensive
        #       than I expected, so I left the tables in-place
        conn.execute('''create table cluster_stats (iteration int, cluster int,
                        num_rows int, num_cols int, residual decimal)''')
        conn.execute('create table statstypes (category text, name text)')
        conn.execute("insert into statstypes values ('main', 'fuzzy_coeff')")
        conn.execute("insert into statstypes values ('main', 'median_residual')")
        conn.execute('''create table iteration_stats (statstype int, iteration int, score decimal)''')

        conn.execute('''create table row_names (order_num int, name text)''')
        conn.execute('''create table column_names (order_num int, name text)''')

        # result tables
        conn.execute('''create table row_members (iteration int, cluster int,
                        order_num int)''')
        conn.execute('''create table column_members (iteration int, cluster int,
                        order_num int)''')
        conn.execute('create table global_background (subsequence text, pvalue decimal)')

        # in case you are wondering about the redundant iteration field here -
        # it allows for much faster database access when selecting by iteration
        conn.execute('''create table motif_infos (iteration int, cluster int,
                        seqtype text, motif_num int, evalue decimal)''')
        conn.execute('''create table motif_pssm_rows (motif_info_id int,
                        iteration int, row int, a decimal, c decimal, g decimal,
                        t decimal)''')

        # Additional info: MEME generated top matching sites
        conn.execute('''create table meme_motif_sites (motif_info_id int,
                        seq_name text,
                        reverse boolean, start int, pvalue decimal,
                        flank_left text, seq text, flank_right text)''')
        # Additional TomTom step
        conn.execute('''create table tomtom_results (motif_info_id1 int,
                        motif_info_id2 int, pvalue decimal)''')

        conn.execute('''create table motif_annotations (motif_info_id int,
                        iteration int, gene_num int,
                        position int, reverse boolean, pvalue decimal)''')
        conn.execute('''create index if not exists colmemb_iter_index
                        on column_members (iteration)''')
        conn.execute('''create index if not exists rowmemb_iter_index
                        on row_members (iteration)''')
        conn.execute('''create index if not exists cluststat_iter_index
                        on cluster_stats (iteration)''')
        conn.execute("create index if not exists rnames_name_index on row_names (name)")
        conn.execute("create index if not exists rowmemb_order_index on row_members (order_num)")
        conn.execute("create index if not exists rowmemb_clust_index on row_members (cluster)")
        conn.execute("create index if not exists motinf_clust_index on motif_infos (cluster)")

        logging.debug("created output database schema")

        # all cluster members are stored relative to the base ratio matrix
        with conn:
            for index in xrange(len(self.ratios.row_names)):
                conn.execute('''insert into row_names (order_num, name) values
                                (?,?)''',
                             (index, self.ratios.row_names[index]))
            for index in xrange(len(self.ratios.column_names)):
                conn.execute('''insert into column_names (order_num, name) values
                                (?,?)''',
                             (index, self.ratios.column_names[index]))
        logging.debug("added row and column names to output database")

    def report_params(self):
        logging.info('cmonkey_run config_params:')
        for param, value in self.config_params.items():
            logging.info('%s=%s' % (param, str(value)))

    def __getitem__(self, key):
        return self.config_params[key]

    def __setitem__(self, key, value):
        self.config_params[key] = value

    def __make_membership(self):
        """returns the seeded membership on demand"""
        if 'random_seed' in self['debug']:
            util.r_set_seed(10)

        new_membs = memb.create_membership(self.ratios,
                               self.row_seeder, self.column_seeder,
                               self.config_params)
        return new_membs

    def membership(self):
        if self.__membership is None:
            logging.debug("creating and seeding memberships")
            self.__membership = self.__make_membership()

            # debug: write seed into an analytical file for iteration 0
            if 'random_seed' in self['debug']:
                conn = self.__dbconn()
                with conn:
                    self.write_memberships(conn, 0)
                # write complete result into a cmresults.tsv
                path =  os.path.join(self['output_dir'], 'cmresults-0000.tsv.bz2')
                with bz2.BZ2File(path, 'w') as outfile:
                    debug.write_iteration(conn, outfile, 0,
                                          self['num_clusters'], self['output_dir'])

        return self.__membership

    def organism(self):
        """returns the organism object to work on"""
        if self.use_dummy_organism():
            self.__organism = org.DummyOrganism()
        elif self.__organism is None:
            self.__organism = self.make_organism()
        return self.__organism

    def __get_kegg_data(self):
        # determine the NCBI code
        organism_code = self['organism_code']

        try:
            kegg_path = resource_filename(Requirement.parse("cmonkey2"), USER_KEGG_FILE_PATH)
        except DistributionNotFound:
            kegg_path = USER_KEGG_FILE_PATH

        keggfile = util.read_dfile(kegg_path, comment='#')
        kegg_map = util.make_dfile_map(keggfile, 1, 3)
        kegg2ncbi = util.make_dfile_map(keggfile, 1, 2)
        if self['ncbi_code'] is None and organism_code in kegg2ncbi:
            self['ncbi_code'] = kegg2ncbi[organism_code]
        return self['ncbi_code'], kegg_map[organism_code]

    def make_organism(self):
        """returns the organism object to work on"""
        self.__make_dirs_if_needed()
        ncbi_code, kegg_species = self.__get_kegg_data()

        try:
            go_file_path = resource_filename(Requirement.parse("cmonkey2"), USER_GO_FILE_PATH)
        except DistributionNotFound:
            go_file_path = USER_GO_FILE_PATH

        gofile = util.read_dfile(go_file_path)

        if self['rsat_dir']:
            if not self['rsat_organism']:
                raise Exception('override RSAT loading: please specify --rsat_organism')
            logging.info("using RSAT files for '%s'", self['rsat_organism'])
            rsatdb = rsat.RsatFiles(self['rsat_dir'], self['rsat_organism'], ncbi_code, self['rsat_features'], self['rsat_base_url'])
        else:
            rsatdb = rsat.RsatDatabase(self['rsat_base_url'], self['cache_dir'], kegg_species, ncbi_code, self['rsat_features'])

        if self['operon_file']:
            logging.info("using operon file at '%s'", self['operon_file'])
            mo_db = microbes_online.MicrobesOnlineOperonFile(self['operon_file'])
        else:
            logging.info("attempting automatic download of operons from Microbes Online")
            mo_db = microbes_online.MicrobesOnline(self['cache_dir'])

        stringfile = self['string_file']
        nw_factories = []
        is_microbe = self['organism_code'] not in VERTEBRATES

        # determine the final weights. note: for now, we will just check whether
        # we have 1 or 2 networks
        num_networks = 0
        if not self['nonetworks'] and self['use_string']:
            num_networks += 1
        if is_microbe and not self['nonetworks'] and self['use_operons']:
            num_networks += 1
        network_weight = 0.0
        if num_networks > 0:
            network_weight = 1.0 / num_networks

        # do we use STRING ?
        if not self['nonetworks'] and self['use_string']:
            # download if not provided
            if stringfile is None:
                if ncbi_code is None:
                    rsat_info = org.RsatSpeciesInfo(rsatdb, kegg_species,
                                                    self['rsat_organism'], None)
                    ncbi_code = rsat_info.taxonomy_id

                logging.info("NCBI CODE IS: %s", ncbi_code)
                url = STRING_URL_PATTERN % ncbi_code
                stringfile = "%s/%s.gz" % (self['cache_dir'], ncbi_code)
                self['string_file'] = stringfile
                logging.info("Automatically using STRING file in '%s' (URL: %s)",
                             stringfile, url)
                util.get_url_cached(url, stringfile)
            else:
                logging.info("Loading STRING file at '%s'", stringfile)

            # create and add network
            nw_factories.append(stringdb.get_network_factory(
                self['organism_code'], stringfile, network_weight))

        # do we use operons ?
        if is_microbe and not self['nonetworks'] and self['use_operons']:
            logging.debug('adding operon network factory')
            nw_factories.append(microbes_online.get_network_factory(
                mo_db, max_operon_size=self.ratios.num_rows / 20,
                weight=network_weight))

        orgcode = self['organism_code']
        logging.debug("Creating Microbe object for '%s'", orgcode)
        rsat_info = org.RsatSpeciesInfo(rsatdb, kegg_species, self['rsat_organism'],
                                        ncbi_code)
        gotax = util.make_dfile_map(gofile, 0, 1)[rsat_info.go_species()]
        synonyms = None
        if self['synonym_file'] is not None:
            synonyms = thesaurus.create_from_delimited_file2(self['synonym_file'],
                                                             self['case_sensitive'])

        #New logic: test to see if there's a fastafile.  If not, then
        #Download it from rsat, process it, and then return the new file name

        is_microbe = True
        if is_microbe:
           organism = org.Microbe(orgcode, kegg_species, rsat_info, gotax, mo_db,
                                   nw_factories,
                                   self['search_distances'], self['scan_distances'],
                                   self['use_operons'], self.ratios, synonyms,
                                   self['fasta_file'])
        else:
            organism = org.RSATOrganism(orgcode, kegg_species, rsat_info, gotax,
                                        nw_factories,
                                        self['search_distances'], self['scan_distances'],
                                        self.ratios, synonyms,
                                        self['fasta_file'])

        conn = self.__dbconn()
        with conn:
            for network in organism.networks():
                conn.execute("insert into statstypes values ('network',?)", [network.name])
            for sequence_type in self['sequence_types']:
                conn.execute("insert into statstypes values ('seqtype',?)", [sequence_type])

        return organism


    def __make_dirs_if_needed(self):
        logging.debug('creating aux directories')
        output_dir = self['output_dir']
        if not os.path.exists(output_dir):
            os.mkdir(output_dir)

        cache_dir = self['cache_dir']
        if not os.path.exists(cache_dir):
            os.mkdir(cache_dir)

    def __clear_output_dir(self):
        output_dir = self['output_dir']
        if os.path.exists(output_dir):
            outfiles = os.listdir(output_dir)
            for filename in outfiles:
                os.remove('/'.join([output_dir, filename]))

    def __check_parameters(self):
        """ensure that we all required parameters before we start running"""
        PARAM_NAMES = ['num_iterations', 'start_iteration', 'multiprocessing',
                       'quantile_normalize',
                       'memb.min_cluster_rows_allowed', 'memb.max_cluster_rows_allowed',
                       'memb.prob_row_change', 'memb.prob_col_change',
                       'memb.max_changes_per_row', 'memb.max_changes_per_col',
                       'sequence_types', 'search_distances', 'scan_distances']

        for param in PARAM_NAMES:
            if param not in self.config_params:
                raise Exception("required parameter not found in config: '%s'" % param)


    def __setup_pipeline(self):
        """Reading pipeline setup
        By default, this uses the default pipelines defined in config
        The default pipeline can be modified by
        1. nomotifs switch
        2. nonetworks switch

        User-defined pipelines can be provided using a JSON file, which is
        specified using the --pipeline switch on the command line
        """
        pipeline_id = 'default'
        if self['nonetworks'] and self['nomotifs']:
            pipeline_id = 'rows'
        elif self['nonetworks']:
            pipeline_id = 'rowsandmotifs'
        elif self['nomotifs']:
            pipeline_id = 'rowsandnetworks'

        if self['pipeline_file']:
            pipeline_file = self['pipeline_file']
            if os.path.exists(pipeline_file):
                with open(pipeline_file) as infile:
                    self['pipeline'] = json.load(infile)
            else:
                raise Exception("Pipeline file '%s' does not exist" % pipeline_file)
        else:
            try:
                pipeline_path = resource_filename(Requirement.parse("cmonkey2"),
                                                  PIPELINE_USER_PATHS[pipeline_id])
            except DistributionNotFound:
                pipeline_path = PIPELINE_USER_PATHS[pipeline_id]

            with open(pipeline_path) as infile:
                self['pipeline'] = json.load(infile)

        # TODO: for now, we always assume the top level of row scoring is a combiner
        class_ = get_function_class(self['pipeline']['row-scoring']['function'])
        if class_.__name__ == 'ScoringFunctionCombiner':
            funs = [get_function_class(fun['function'])(self.organism(),
                                                       self.membership(),
                                                       self.ratios,
                                                       self.config_params)
                    for fun in self['pipeline']['row-scoring']['args']['functions']]
            row_scoring = class_(self.organism(), self.membership(), funs, self.config_params)
        else:
            raise Exception('Row scoring top level must be ScoringFunctionCombiner')

        # column scoring
        class_ = get_function_class(self['pipeline']['column-scoring']['function'])
        col_scoring = class_(self.organism(), self.membership(), self.ratios,
                             config_params=self.config_params)
        return row_scoring, col_scoring

    def use_dummy_organism(self):
        """check whether we use a dummy organism"""
        return (self['organism_code'] is None and
                self['nonetworks'] and self['nomotifs'])

    def prepare_run(self, check_params=True):
        """Setup output directories and scoring functions for the scoring.
        Separating setup and actual run facilitates testing"""
        if check_params:
            self.__check_parameters()

        if not self['resume']:
            self.__make_dirs_if_needed()
            self.__clear_output_dir()
            self.__create_output_database()

            # write the normalized ratio matrix for stats and visualization
            output_dir = self['output_dir']
            if not os.path.exists(os.path.join(output_dir, '/ratios.tsv')):
                self.ratios.write_tsv_file(output_dir + '/ratios.tsv')
            # also copy the input matrix to the output
            if (os.path.exists(self['ratios_file'])):
                if self['ratios_file'].endswith('.gz'):
                    copy_name = 'ratios.original.tsv.gz'
                else:
                    copy_name = 'ratios.original.tsv'

                shutil.copyfile(self['ratios_file'],
                                os.path.join(output_dir, 'ratios.original.tsv'))

        # gene index map is used for writing statistics
        thesaurus = self.organism().thesaurus()
        genes = [thesaurus[row_name] if row_name in thesaurus else row_name
                 for row_name in self.ratios.row_names]
        self.gene_indexes = {genes[index]: index
                             for index in xrange(len(genes))}
        row_scoring, col_scoring = self.__setup_pipeline()
        row_scoring.check_requirements()
        col_scoring.check_requirements()

        config.write_setup(self.config_params)

        self.row_scoring = row_scoring
        self.column_scoring = col_scoring

        ## MOVED FROM run_iterations()
        self.report_params()
        self.write_start_info()

        conn = self.__dbconn()
        with conn:
            for scoring_function in self.row_scoring.scoring_functions:
                conn.execute("insert into statstypes values ('scoring',?)", [scoring_function.id])
            conn.execute("insert into statstypes values ('scoring',?)", [self.column_scoring.id])

        if 'profile_mem' in self['debug']:
            with open(os.path.join(self['output_dir'], 'memprofile.tsv'), 'w') as outfile:
                outfile.write('Iteration\tMembership\tOrganism\tCol\tRow\tNetwork\tMotif\n')
        ## end MOVED

        if self['resume']:
            self['start_iteration'] = self.get_last_iteration()

        ##return row_scoring, col_scoring

    def run(self):
        #row_scoring, col_scoring = self.prepare_run()
        #self.row_scoring = row_scoring
        #self.column_scoring = col_scoring
        self.prepare_run()
        self.run_iterations()

    def residual_for(self, row_names, column_names):
        if len(column_names) <= 1 or len(row_names) <= 1:
            return 1.0
        else:
            matrix = self.ratios.submatrix_by_name(row_names, column_names)
            return matrix.residual()

    def write_memberships(self, conn, iteration):
        for cluster in range(1, self['num_clusters'] + 1):
            column_names = self.membership().columns_for_cluster(cluster)
            for order_num in self.ratios.column_indexes_for(column_names):
                conn.execute('''insert into column_members (iteration,cluster,order_num)
                                values (?,?,?)''', (iteration, cluster, order_num))

            row_names = self.membership().rows_for_cluster(cluster)
            for order_num in self.ratios.row_indexes_for(row_names):
                conn.execute('''insert into row_members (iteration,cluster,order_num)
                                values (?,?,?)''', (iteration, cluster, order_num))

    def write_results(self, iteration_result):
        """write iteration results to database"""
        iteration = iteration_result['iteration']
        conn = self.__dbconn()
        with conn:
            self.write_memberships(conn, iteration)

        if 'motifs' in iteration_result:
            motifs = iteration_result['motifs']
            with conn:
                for seqtype in motifs:
                    for cluster in motifs[seqtype]:
                        motif_infos = motifs[seqtype][cluster]['motif-info']
                        for motif_info in motif_infos:
                            c = conn.cursor()
                            c.execute('''insert into motif_infos (iteration,cluster,seqtype,motif_num,evalue)
                                        values (?,?,?,?,?)''',
                                      (iteration, cluster, seqtype, motif_info['motif_num'],
                                       motif_info['evalue']))
                            motif_info_id = c.lastrowid
                            c.close()
                            pssm_rows = motif_info['pssm']
                            for row in xrange(len(pssm_rows)):
                                pssm_row = pssm_rows[row]
                                conn.execute('''insert into motif_pssm_rows (motif_info_id,iteration,row,a,c,g,t)
                                                values (?,?,?,?,?,?,?)''',
                                             (motif_info_id, iteration, row, pssm_row[0], pssm_row[1],
                                              pssm_row[2], pssm_row[3]))
                            annotations = motif_info['annotations']
                            for annotation in annotations:
                                gene_num = self.gene_indexes[annotation['gene']]
                                conn.execute('''insert into motif_annotations (motif_info_id,
                                                iteration,gene_num,
                                                position,reverse,pvalue) values (?,?,?,?,?,?)''',
                                             (motif_info_id, iteration, gene_num,
                                              annotation['position'],
                                              annotation['reverse'], annotation['pvalue']))

                            sites = motif_info['sites']
                            if len(sites) > 0 and isinstance(sites[0], tuple):
                                for seqname, strand, start, pval, flank_left, seq, flank_right in sites:
                                    conn.execute('''insert into meme_motif_sites (motif_info_id, seq_name, reverse, start, pvalue, flank_left, seq, flank_right)
                                                    values (?,?,?,?,?,?,?,?)''',
                                                 (motif_info_id, seqname, strand == '-',
                                                  start, pval, flank_left, seq,
                                                  flank_right))

    def write_stats(self, iteration_result):
        # write stats for this iteration
        iteration = iteration_result['iteration']

        network_scores = iteration_result['networks'] if 'networks' in iteration_result else {}
        motif_pvalues = iteration_result['motif-pvalue'] if 'motif-pvalue' in iteration_result else {}
        fuzzy_coeff = iteration_result['fuzzy-coeff'] if 'fuzzy-coeff' in iteration_result else 0.0

        residuals = []
        conn = self.__dbconn()
        cur = conn.cursor()
        with conn:
            for cluster in range(1, self['num_clusters'] + 1):
                row_names = self.membership().rows_for_cluster(cluster)
                column_names = self.membership().columns_for_cluster(cluster)
                residual = self.residual_for(row_names, column_names)
                residuals.append(residual)
                try:
                    conn.execute('''insert into cluster_stats (iteration, cluster, num_rows,
                                    num_cols, residual) values (?,?,?,?,?)''',
                                 (iteration, cluster, len(row_names), len(column_names),
                                  residual))
                except:
                    # residual is messed up, insert with 1.0
                    logging.warn('STATS: residual was messed up, insert with 1.0')
                    conn.execute('''insert into cluster_stats (iteration, cluster, num_rows,
                                    num_cols, residual) values (?,?,?,?,?)''',
                                 (iteration, cluster, len(row_names), len(column_names),
                                  1.0))

            median_residual = np.median(residuals)
            conn.execute("insert into iteration_stats (statstype,iteration,score) values (?,?,?)",
                         (1, iteration, fuzzy_coeff))

            try:
                conn.execute("insert into iteration_stats (statstype,iteration,score) values (?,?,?)", (2, iteration, median_residual))
            except:
                logging.warn('STATS: median was messed up, insert with 1.0')
                conn.execute("insert into iteration_stats (statstype,iteration,score) values (?,?,?)", (2, iteration, 1.0))

            # insert the score means
            for fun_id in iteration_result['score_means']:
                cur.execute("select rowid from statstypes where category='scoring' and name=?", [fun_id])
                type_id = cur.fetchone()[0]
                conn.execute('insert into iteration_stats (statstype,iteration,score) values (?,?,?)',
                             [type_id, iteration, iteration_result['score_means'][fun_id]])

        with conn:
            for network, score in network_scores.items():
                cur.execute("select rowid from statstypes where category='network' and name=?", [network])
                typeid = cur.fetchone()[0]
                conn.execute("insert into iteration_stats values (?,?,?)",
                             (typeid, iteration, score))
        with conn:
            for seqtype, pval in motif_pvalues.items():
                cur.execute("select rowid from statstypes where category='seqtype' and name=?", [seqtype])
                typeid = cur.fetchone()[0]
                conn.execute("insert into iteration_stats values (?,?,?)",
                             (typeid, iteration, pval))
        cur.close()

    def write_start_info(self):
        conn = self.__dbconn()
        try:
            ncbi_code_int = int(self['ncbi_code'])
        except:
            # this exception happens when ncbi_code is not specified, usually when
            # the data files are provided through the command line (e.g. KBase)
            # in this case, we simply set the code to 0 because it's intended to
            # not matter
            ncbi_code_int = 0

        with conn:
            conn.execute('''insert into run_infos (start_time, num_iterations, organism,
                            species, ncbi_code, num_rows, num_columns, num_clusters, git_sha) values (?,?,?,?,?,?,?,?,?)''',
                         (datetime.now(), self['num_iterations'], self.organism().code,
                          self.organism().species(),
                          ncbi_code_int,
                          self.ratios.num_rows,
                          self.ratios.num_columns, self['num_clusters'],
                          '$Id$'))

    def update_iteration(self, iteration):
        conn = self.__dbconn()
        with conn:
            conn.execute('''update run_infos set last_iteration = ?''', (iteration,))

    def get_last_iteration(self):
        """Return the last iteration listed in cMonkey database.  This is intended to
            inform the '--resume' flag
        """
        try:
            conn = self.__dbconn()
            with conn:
                cur = conn.execute('''select max(last_iteration) from run_infos''')
                iteration = cur.fetchone()[0]
        except:
            iteration = 1
        return iteration

    def write_finish_info(self):
        conn = self.__dbconn()
        with conn:
            conn.execute('''update run_infos set finish_time = ?''', (datetime.now(),))

    def combined_rscores_pickle_path(self):
        return "%s/combined_rscores_last.pkl" % self.config_params['output_dir']

    def run_iteration(self, iteration, force=False):
        """Run a single cMonkey iteration

             Keyword arguments:
             iteration -- The iteration number to run
             force     -- Set to true to force recalculations (DEFAULT:FALSE)
        """
        logging.info("Iteration # %d", iteration)
        iteration_result = {'iteration': iteration, 'score_means': {}}
        if force:
            rscores = self.row_scoring.compute_force(iteration_result)
        else:
            rscores = self.row_scoring.compute(iteration_result)
        start_time = util.current_millis()

        if force:
            cscores = self.column_scoring.compute_force(iteration_result)
        else:
            cscores = self.column_scoring.compute(iteration_result)

        elapsed = util.current_millis() - start_time
        if elapsed > 0.0001:
            logging.debug("computed column_scores in %f s.", elapsed / 1000.0)

        self.membership().update(self.ratios, rscores, cscores,
                                 self['num_iterations'], iteration_result)

        mean_net_score = 0.0
        mean_mot_pvalue = 0.0
        if 'networks' in iteration_result.keys():
            mean_net_score = iteration_result['networks']
        mean_mot_pvalue = "NA"
        if 'motif-pvalue' in iteration_result.keys():
            mean_mot_pvalue = ""
            mean_mot_pvalues = iteration_result['motif-pvalue']
            mean_mot_pvalue = ""
            for seqtype in mean_mot_pvalues.keys():
                mean_mot_pvalue = mean_mot_pvalue + (" '%s' = %f" % (seqtype, mean_mot_pvalues[seqtype]))

        logging.debug('mean net = %s | mean mot = %s', str(mean_net_score), mean_mot_pvalue)

        # Reduce I/O, will write the results to database only on a debug run
        if not self['minimize_io']:
            if iteration == 1 or (iteration % self['result_freq'] == 0):
                self.write_results(iteration_result)

        # This should not be too much writing, so we can keep it OUT of minimize_io option...?
        if iteration == 1 or (iteration % self['stats_freq'] == 0):
            self.write_stats(iteration_result)
            self.update_iteration(iteration)

        if 'dump_results' in self['debug'] and (iteration == 1 or
                                                (iteration % self['debug_freq'] == 0)):
            # write complete result into a cmresults.tsv
            conn = self.__dbconn()
            path =  os.path.join(self['output_dir'], 'cmresults-%04d.tsv.bz2' % iteration)
            with bz2.BZ2File(path, 'w') as outfile:
                debug.write_iteration(conn, outfile, iteration,
                                      self['num_clusters'], self['output_dir'])

    def write_mem_profile(self, outfile, iteration):
        membsize = sizes.asizeof(self.membership()) / 1000000.0
        orgsize = sizes.asizeof(self.organism()) / 1000000.0
        colsize = sizes.asizeof(self.column_scoring) / 1000000.0
        funs = self.row_scoring.scoring_functions
        rowsize = sizes.asizeof(funs[0]) / 1000000.0
        netsize = sizes.asizeof(funs[1]) / 1000000.0
        motsize = sizes.asizeof(funs[2]) / 1000000.0
        outfile.write('%d\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\t%.4f\n' % (iteration, membsize, orgsize, colsize, rowsize, netsize, motsize))

    def run_iterations(self, start_iter=None, num_iter=None):
        if start_iter is None:
            start_iter = self['start_iteration']
        if num_iter is None:
            num_iter=self['num_iterations'] + 1

        if self.config_params['interactive']:  # stop here in interactive mode
            return

        for iteration in range(start_iter, num_iter):
            start_time = util.current_millis()
            force = self['resume'] and iteration == start_iter
            self.run_iteration(iteration, force=force)

            # garbage collection after everything in iteration went out of scope
            gc.collect()
            elapsed = util.current_millis() - start_time
            logging.debug("performed iteration %d in %f s.", iteration, elapsed / 1000.0)

            if 'profile_mem' in self['debug'] and (iteration == 1 or iteration % 100 == 0):
                with open(os.path.join(self['output_dir'], 'memprofile.tsv'), 'a') as outfile:
                    self.write_mem_profile(outfile, iteration)


        """run post processing after the last iteration. We store the results in
        num_iterations + 1 to have a clean separation"""
        if self['postadjust']:
            logging.info("Postprocessing: Adjusting the clusters....")
            # run combiner using the weights of the last iteration

            rscores = self.row_scoring.combine_cached(self['num_iterations'])
            rd_scores = memb.get_row_density_scores(self.membership(), rscores)
            logging.info("Recomputed combined + density scores.")
            memb.postadjust(self.membership(), rd_scores)

            BSCM_obj = self.column_scoring.get_BSCM()
            if not (BSCM_obj is None):
                new_membership = BSCM_obj.resplit_clusters(self.membership(), cutoff=0.05)

            logging.info("Adjusted. Now re-run scoring (iteration: %d)",
                         self['num_iterations'])
            iteration_result = {'iteration': self['num_iterations'] + 1,
                                'score_means': {}}

            combined_scores = self.row_scoring.compute_force(iteration_result)

            # write the combined scores for benchmarking/diagnostics
            with open(self.combined_rscores_pickle_path(), 'wb') as outfile:
                pickle.dump(combined_scores, outfile)

            self.write_results(iteration_result)
            self.write_stats(iteration_result)
            self.update_iteration(iteration)

            # default behaviour:
            # always write complete result into a cmresults.tsv for R/cmonkey
            # compatibility
            conn = self.__dbconn()
            path =  os.path.join(self['output_dir'], 'cmresults-postproc.tsv.bz2')
            with bz2.BZ2File(path, 'w') as outfile:
                debug.write_iteration(conn, outfile,
                                      self['num_iterations'] + 1,
                                      self['num_clusters'], self['output_dir'])
            # TODO: Why is conn never closed?  Where does it write to the db?

            # additionally: run tomtom on the motifs if requested
            if (self['MEME']['global_background'] == 'True' and
                self['Postprocessing']['run_tomtom'] == 'True'):
                meme.run_tomtom(conn, self['output_dir'], self['MEME']['version'])

        self.write_finish_info()
        logging.info("Done !!!!")


def get_function_class(scorefun):
    modulepath = scorefun['module'].split('.')
    if len(modulepath) > 1:
        module = __import__(scorefun['module'], fromlist=[modulepath[1]])
    else:
        module = __import__(modulepath[0])
    return getattr(module, scorefun['class'])
