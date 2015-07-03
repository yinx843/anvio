# coding: utf-8
"""Provides the necessary class to profile BAM files."""

import os
import sys
import pysam
import shutil

import anvio
import anvio.tables as t
import anvio.dbops as dbops
import anvio.utils as utils
import anvio.dictio as dictio
import anvio.terminal as terminal
import anvio.constants as constants
import anvio.clustering as clustering
import anvio.filesnpaths as filesnpaths

from anvio.errors import ConfigError
from anvio.metadata import Metadata
from anvio.contig import Split, Contig, set_contigs_abundance
from anvio.clusteringconfuguration import ClusteringConfiguration


__author__ = "A. Murat Eren"
__copyright__ = "Copyright 2015, The anvio Project"
__credits__ = []
__license__ = "GPL 3.0"
__version__ = anvio.__version__
__maintainer__ = "A. Murat Eren"
__email__ = "a.murat.eren@gmail.com"
__status__ = "Development"


pp = terminal.pretty_print


class BAMProfiler:
    """Creates an über class for BAM file operations"""
    def __init__(self, args = None):
        self.args = None
        self.input_file_path = None 
        self.annotation_db_path = None
        self.serialized_profile_path = None 
        self.split_length = 20000
        self.output_directory = None 
        self.list_contigs_and_exit = None 
        self.min_contig_length = 10000 
        self.min_mean_coverage = 0
        self.min_coverage_for_variability = 10 # if a nucleotide position is covered less than this, don't bother
        self.contig_names_of_interest = None
        self.contigs_shall_be_clustered = False
        self.report_variability_full = False # don't apply any noise filtering, and simply report ALL base frequencies
        self.overwrite_output_destinations = False

        if args:
            self.args = args
            self.input_file_path = args.input_file
            self.annotation_db_path = args.annotation_db_path
            self.serialized_profile_path = args.profile
            self.output_directory = args.output_directory
            self.list_contigs_and_exit = args.list_contigs
            self.min_contig_length = args.min_contig_length
            self.min_mean_coverage = args.min_mean_coverage
            self.min_coverage_for_variability = args.min_coverage_for_variability
            self.contigs_shall_be_clustered = args.cluster_contigs
            self.number_of_threads = 4 
            self.no_trehading = True
            self.sample_id = args.sample_id
            self.report_variability_full = args.report_variability_full
            self.overwrite_output_destinations = args.overwrite_output_destinations

            if args.contigs_of_interest:
                if not os.path.exists(args.contigs_of_interest):
                    raise ConfigError, "Contigs file (%s) is missing..." % (args.contigs_of_interest)

                self.contig_names_of_interest = set([c.strip() for c in open(args.contigs_of_interest).readlines()\
                                                                               if c.strip() and not c.startswith('#')])

        self.bam = None
        self.contigs = {}
        self.genes_in_contigs = {}
        self.contig_names_in_annotation_db = None

        self.database_paths = {'ANNOTATION.db': self.annotation_db_path}

        self.profile_db_path = None

        self.clustering_configs = constants.clustering_configs['single']

        self.progress = terminal.Progress()
        self.run = terminal.Run(width=35)

        self.metadata = Metadata(self.progress)

        # following variable will be populated during the profiling, and its content will eventually
        # be stored in t.variable_positions_table_name
        self.variable_positions_table_entries = []


    def init_dirs_and_dbs(self):
        if not self.annotation_db_path:
            raise ConfigError, "You can not run profiling without an annotation database. You can create\
                                      one using 'anvi-gen-annotation-database'. Not sure how? Please see the\
                                      user manual."

        self.output_directory = filesnpaths.check_output_directory(self.output_directory or self.input_file_path + '-ANVIO_PROFILE',\
                                                                   ok_if_exists = self.overwrite_output_destinations)

        self.progress.new('Initializing')

        self.progress.update('Creating the output directory ...')
        filesnpaths.gen_output_directory(self.output_directory, self.progress, delete_if_exists = self.overwrite_output_destinations)

        self.progress.update('Initializing the annotation database ...')
        annotation_db = dbops.AnnotationDatabase(self.annotation_db_path)
        self.split_length = int(annotation_db.meta['split_length'])
        self.annotation_hash = annotation_db.meta['annotation_hash']
        self.contig_names_in_annotation_db = set(annotation_db.db.get_table_as_dict(t.contigs_info_table_name, string_the_key = True).keys())
        annotation_db.disconnect()

        self.progress.update('Creating a new single profile database with annotation hash "%s" ...' % self.annotation_hash)
        self.profile_db_path = self.generate_output_destination('PROFILE.db')
        profile_db = dbops.ProfileDatabase(self.profile_db_path)

        meta_values = {'db_type': 'profile',
                       'sample_id': self.sample_id,
                       'samples': self.sample_id,
                       'merged': False,
                       'contigs_clustered': self.contigs_shall_be_clustered,
                       'min_coverage_for_variability': self.min_coverage_for_variability,
                       'default_view': 'single',
                       'min_contig_length': self.min_contig_length,
                       'report_variability_full': self.report_variability_full,
                       'annotation_hash': self.annotation_hash}
        profile_db.create(meta_values)

        self.progress.end()


    def _run(self):
        self.check_args()

        self.set_sample_id()

        if self.list_contigs_and_exit:
            self.list_contigs()
            sys.exit()

        self.init_dirs_and_dbs()

        # we will set up things here so the information in the annotation_db
        # can be utilized directly from within the contigs for loop. contig to
        # gene associations will be stored in self.genes_in_contigs dictionary for
        # fast access.
        if self.annotation_db_path:
            self.populate_genes_in_contigs_dict()

        self.run.info('anvio', anvio.__version__)
        self.run.info('profiler_version', anvio.__profile__version__)
        self.run.info('sample_id', self.sample_id)
        self.run.info('profile_db', self.profile_db_path, display_only = True)
        self.run.info('annotation_db', True if self.annotation_db_path else False)
        self.run.info('annotation_hash', self.annotation_hash)
        self.run.info('cmd_line', utils.get_cmd_line())
        self.run.info('merged', False)
        self.run.info('split_length', self.split_length)
        self.run.info('min_contig_length', self.min_contig_length)
        self.run.info('min_mean_coverage', self.min_mean_coverage)
        self.run.info('clustering_performed', self.contigs_shall_be_clustered)
        self.run.info('min_coverage_for_variability', self.min_coverage_for_variability)
        self.run.info('report_variability_full', self.report_variability_full)

        # this is kinda important. we do not run full-blown profile function if we are dealing with a summarized
        # profile...
        if self.input_file_path:
            self.init_profile_from_BAM()
            self.profile()
            self.store_profile()
            self.store_summarized_profile_for_each_split()
        else:
            self.init_serialized_profile()
            self.store_summarized_profile_for_each_split()

        self.generate_variabile_positions_table()
        self.generate_gene_coverages_table()

        # here we store both metadata and TNF information into the database:
        profile_db = dbops.ProfileDatabase(self.profile_db_path, quiet=True)
        self.metadata.store_metadata_for_contigs_and_splits(self.sample_id, self.contigs, profile_db.db)
        profile_db.disconnect()

        # the only view for the single PROFILE database is ready, and already
        # set as the default view. store the info in the db:
        views_table = dbops.TableForViews(self.profile_db_path, anvio.__profile__version__)
        views_table.append('single', 'metadata_splits')
        views_table.store()

        if self.contigs_shall_be_clustered:
            self.cluster_contigs()

        runinfo_serialized = self.generate_output_destination('RUNINFO.cp')
        self.run.info('runinfo', runinfo_serialized)
        self.run.store_info_dict(runinfo_serialized, strip_prefix = self.output_directory)

        self.run.quit()


    def populate_genes_in_contigs_dict(self):
        self.progress.new('Annotation')
        self.progress.update('Reading genes in contigs table')
        annotation_db = dbops.AnnotationDatabase(self.annotation_db_path)
        genes_in_contigs_table = annotation_db.db.get_table_as_dict(t.genes_contigs_table_name, t.genes_contigs_table_structure)
        annotation_db.disconnect()

        self.progress.update('Populating ORFs dictionary for each contig ...')
        for gene in genes_in_contigs_table:
            e = genes_in_contigs_table[gene]
            if self.genes_in_contigs.has_key(e['contig']):
                self.genes_in_contigs[e['contig']].add((gene, e['start'], e['stop']), )
            else:
                self.genes_in_contigs[e['contig']] = set([(gene, e['start'], e['stop']), ])

        self.progress.end()
        self.run.info('annotation_db', "%d genes processed successfully." % len(genes_in_contigs_table), display_only = True)


    def generate_variabile_positions_table(self):
        variable_positions_table = dbops.TableForVariability(self.profile_db_path, anvio.__profile__version__, progress = self.progress)

        self.progress.new('Storing variability information')
        for contig in self.contigs.values():
            for split in contig.splits:
                for column_profile in split.column_profiles.values():
                    column_profile['sample_id'] = self.sample_id
                    variable_positions_table.append(column_profile)

        variable_positions_table.store()
        self.progress.end()
        self.run.info('variable_positions_table', True, quiet = True)


    def generate_gene_coverages_table(self):
        gene_coverages_table = dbops.TableForGeneCoverages(self.profile_db_path, anvio.__profile__version__, progress = self.progress)

        self.progress.new('Profiling genes')
        num_contigs = len(self.contigs)
        contig_names = self.contigs.keys()
        for i in range(0, num_contigs):
            contig = contig_names[i]
            self.progress.update('Processing contig %d of %d' % (i + 1, num_contigs))
            # if no open reading frames were found in a contig, it wouldn't have an entry in the annotation table,
            # therefore there wouldn't be any record of it in contig_ORFs; so we better check ourselves before
            # we wreck ourselves and the ultimately the analysis of this poor user:
            if self.genes_in_contigs.has_key(contig):
                gene_coverages_table.analyze_contig(self.contigs[contig], self.sample_id, self.genes_in_contigs[contig])

        gene_coverages_table.store()
        self.progress.end()
        self.run.info('gene_coverages_table', True, quiet = True)


    def set_sample_id(self):
        if self.sample_id:
            utils.check_sample_id(self.sample_id)
        else:
            if self.input_file_path:
                self.input_file_path = os.path.abspath(self.input_file_path)
                self.sample_id = os.path.basename(self.input_file_path).upper().split('.BAM')[0]
                self.sample_id = self.sample_id.replace('-', '_')
                self.sample_id = self.sample_id.replace('.', '_')
                if self.sample_id[0] in constants.digits:
                    self.sample_id = 's' + self.sample_id
                utils.check_sample_id(self.sample_id)
            if self.serialized_profile_path:
                self.serialized_profile_path = os.path.abspath(self.serialized_profile_path)
                self.sample_id = os.path.basename(os.path.dirname(self.serialized_profile_path))


    def check_contigs_without_any_ORFs(self, contig_names):
        if not self.annotation_db_path:
            return
        contig_names = set(contig_names)
        contigs_without_annotation = [c for c in contig_names if c not in self.genes_in_contigs]

        if len(contigs_without_annotation):
            import random
            P = lambda x: 'are %d contigs' % (x) if x > 1 else 'there is one contig'
            self.run.warning('You have instructed profiling to use an annotation database,\
                              however, there %s in your BAM file that did not get annotated. Which means\
                              whatever method you used to identify open reading frames in these contigs\
                              failed to find any open reading frames in those. Which may be normal\
                              (a) if your contigs are very short, or (b) if your gene finder is not\
                              capable of dealing with your stuff. If you know what you are doing, that\
                              is fine. Otherwise please double check. Here is one contig missing\
                              annotation if you would like to play: %s"' %\
                                      (P(len(contigs_without_annotation)), random.choice(contigs_without_annotation)))


    def init_serialized_profile(self):
        self.progress.new('Init')
        self.progress.update('Reading serialized profile')
        self.contigs = dictio.read_serialized_object(self.serialized_profile_path)
        self.progress.end()

        self.run.info('profile_loaded_from', self.serialized_profile_path)
        self.run.info('num_contigs', pp(len(self.contigs)))

        if self.contig_names_of_interest:
            contigs_to_discard = set()
            for contig in self.contigs:
                if contig not in self.contig_names_of_interest:
                    contigs_to_discard.add(contig)

            if len(contigs_to_discard):
                for contig in contigs_to_discard:
                    self.contigs.pop(contig)
            self.run.info('num_contigs_selected_for_analysis', pp(len(self.contigs)))

        self.check_contigs()

        # it brings good karma to let the user know what the hell is wrong with their data:
        self.check_contigs_without_any_ORFs(self.contigs.keys())

        contigs_to_discard = set()
        for contig in self.contigs.values():
            if contig.length < self.min_contig_length:
                contigs_to_discard.add(contig.name)
        if len(contigs_to_discard):
            for contig in contigs_to_discard:
                self.contigs.pop(contig)
            self.run.info('contigs_raw_longer_than_M', len(self.contigs))

        self.check_contigs()


    def list_contigs(self):
        if self.input_file_path:
            self.progress.new('Init')
            self.progress.update('Reading BAM File')
            self.bam = pysam.Samfile(self.input_file_path, 'rb')
            self.progress.end()

            self.contig_names = self.bam.references
            self.contig_lenghts = self.bam.lengths

            utils.check_contig_names(self.contig_names)

            for tpl in sorted(zip(self.contig_lenghts, self.contig_names), reverse = True):
                print '%-40s %s' % (tpl[1], pp(int(tpl[0])))

        else:
            self.progress.new('Init')
            self.progress.update('Reading serialized profile')
            self.contigs = dictio.read_serialized_object(self.serialized_profile_path)
            self.progress.end()

            self.run.info('profile_loaded_from', self.serialized_profile_path)
            self.run.info('num_contigs', pp(len(self.contigs)))

            for tpl in sorted([(int(self.contigs[contig].length), contig) for contig in self.contigs]):
                print '%-40s %s' % (tpl[1], pp(int(tpl[0])))


    def init_profile_from_BAM(self):
        self.progress.new('Init')
        self.progress.update('Reading BAM File')
        try:
            self.bam = pysam.Samfile(self.input_file_path, 'rb')
        except ValueError as e:
            self.progress.end()
            raise ConfigError, 'Are you sure "%s" is a BAM file? Because samtools is not happy with it: """%s"""' % (self.input_file_path, e)
        self.progress.end()

        self.contig_names = self.bam.references
        self.contig_lenghts = self.bam.lengths

        utils.check_contig_names(self.contig_names)

        try:
            self.num_reads_mapped = self.bam.mapped
        except ValueError:
            raise ConfigError, "It seems the BAM file is not indexed. See 'anvi-init-bam' script."

        runinfo = self.generate_output_destination('RUNINFO')
        self.run.init_info_file_obj(runinfo)
        self.run.info('input_bam', self.input_file_path)
        self.run.info('output_dir', self.output_directory, display_only = True)
        self.run.info('total_reads_mapped', pp(int(self.num_reads_mapped)))
        self.run.info('num_contigs', pp(len(self.contig_names)))

        if self.contig_names_of_interest:
            indexes = [self.contig_names.index(r) for r in self.contig_names_of_interest if r in self.contig_names]
            self.contig_names = [self.contig_names[i] for i in indexes]
            self.contig_lenghts = [self.contig_lenghts[i] for i in indexes]
            self.run.info('num_contigs_selected_for_analysis', pp(len(self.contig_names)))

        # it brings good karma to let the user know what the hell is wrong with their data:
        self.check_contigs_without_any_ORFs(self.contig_names)

        # check for the -M parameter.
        contigs_longer_than_M = set()
        for i in range(0, len(self.contig_names)):
            if self.contig_lenghts[i] >= self.min_contig_length:
                contigs_longer_than_M.add(i)

        if not len(contigs_longer_than_M):
            raise ConfigError, "0 contigs larger than %s nts." % pp(self.min_contig_length)
        else:
            self.contig_names = [self.contig_names[i] for i in contigs_longer_than_M]
            self.contig_lenghts = [self.contig_lenghts[i] for i in contigs_longer_than_M]
            self.num_contigs = len(self.contig_names)    # we will store these two
            self.total_length = sum(self.contig_lenghts) # into the db in a second.


        # let's see whether the user screwed up to follow the simple instructions
        # mentioned here: http://merenlab.org/2015/05/01/anvio-tutorial/#preparation
        for contig_name in self.contig_names:
            if contig_name not in self.contig_names_in_annotation_db:
                raise ConfigError, "At least one contig name in your BAM file does not match contig names stored in the\
                                    annotation database. For instance, this is one contig name found in your BAM file: '%s',\
                                    and this is another one found in your annotation database: '%s'. You may be using an\
                                    annotation database for profiling that has nothing to do with the BAM file you are\
                                    trying to profile, or you may have failed to fix your contig names in your FASTA file\
                                    prior to mapping, which is described here: %s"\
                                        % (contig_name, self.contig_names_in_annotation_db.pop(), 'http://goo.gl/Q9ChpS')

        # finally, compute contig splits.
        annotation_db = dbops.AnnotationDatabase(self.annotation_db_path)
        self.splits_in_annotation_db = annotation_db.db.get_table_as_dict(t.splits_info_table_name)
        annotation_db.disconnect()

        contigs_longer_than_M = set(self.contig_names) # for fast access
        self.split_names = set([])
        self.contig_name_to_splits = {}
        for split_name in self.splits_in_annotation_db:
            parent = self.splits_in_annotation_db[split_name]['parent']

            if parent not in contigs_longer_than_M:
                continue

            self.split_names.add(split_name)

            if self.contig_name_to_splits.has_key(parent):
                self.contig_name_to_splits[parent].append(split_name)
            else:
                self.contig_name_to_splits[parent] = [split_name]

        # we just recovered number of splits that are coming from contigs
        # longer than M:
        self.num_splits = len(self.split_names)

        self.run.info('num_contigs_after_M', self.num_contigs, display_only = True)
        self.run.info('num_contigs', self.num_contigs, quiet = True)
        self.run.info('num_splits', self.num_splits)
        self.run.info('total_length', self.total_length)

        profile_db = dbops.ProfileDatabase(self.profile_db_path, quiet=True)
        profile_db.db.set_meta_value('num_splits', self.num_splits)
        profile_db.db.set_meta_value('num_contigs', self.num_contigs)
        profile_db.db.set_meta_value('total_length', self.total_length)
        profile_db.db.set_meta_value('total_reads_mapped', int(self.num_reads_mapped))
        profile_db.disconnect()


    def generate_output_destination(self, postfix, directory = False):
        return_path = os.path.join(self.output_directory, postfix)

        if directory == True:
            if os.path.exists(return_path):
                shutil.rmtree(return_path)
            os.makedirs(return_path)

        return return_path


    def profile(self):
        """Big deal function"""

        # So we start with essential stats. In the section below, we will simply go through each contig
        # in the BAM file and populate the contigs dictionary for the first time.
        for i in range(0, len(self.contig_names)):
        
            contig_name = self.contig_names[i]

            contig = Contig(contig_name)
            contig.length = self.contig_lenghts[i]
            contig.split_length = self.split_length
            contig.min_coverage_for_variability = self.min_coverage_for_variability
            contig.report_variability_full = self.report_variability_full

            self.progress.new('Profiling "%s" (%d of %d) (%s nts)' % (contig.name,
                                                                      i + 1,
                                                                      len(self.contig_names),
                                                                      pp(int(contig.length))))

            # populate contig with empty split objects and 
            for split_name in self.contig_name_to_splits[contig_name]:
                s = self.splits_in_annotation_db[split_name]
                split = Split(split_name, contig_name, s['order_in_parent'], s['start'], s['end'])
                contig.splits.append(split)

            # analyze coverage for each split
            contig.analyze_coverage(self.bam, self.progress)

            # test the mean coverage of the contig.
            discarded_contigs_due_to_C = set([])
            if contig.coverage.mean < self.min_mean_coverage:
                # discard this contig and continue
                discarded_contigs_due_to_C.add(contig.name)
                self.progress.end()
                continue

            contig.analyze_auxiliary(self.bam, self.progress)

            self.progress.end()

            # add contig to the dict.
            self.contigs[contig_name] = contig


        if discarded_contigs_due_to_C:
            self.run.info('contigs_after_C', pp(len(self.contigs)))

        # set contig abundance
        set_contigs_abundance(self.contigs)

        self.check_contigs()


    def store_profile(self):
        output_file = self.generate_output_destination('PROFILE.cp')
        self.progress.new('Storing Profile')
        self.progress.update('Serializing information for %s contigs ...' % pp(len(self.contigs)))
        dictio.write_serialized_object(self.contigs, output_file)
        self.progress.end()
        self.run.info('profile_dict', output_file)


    def store_summarized_profile_for_each_split(self):
        summary_index = {}
        summary_index_output_path = self.generate_output_destination('SUMMARY.cp')
        summary_dir = self.generate_output_destination('SUMMARY', directory=True)
        self.progress.new('Storing summary files')

        counter = 1

        for contig in self.contigs:
            self.progress.update('working on contig %s of %s' % (pp(counter), pp(len(self.contigs))))
            for split in self.contigs[contig].splits:
                split_summary_path = self.generate_output_destination(os.path.join(summary_dir, '%.6d.cp' % counter))
                dictio.write_serialized_object({self.sample_id: {'coverage': split.coverage.c,
                                                                 'variability': split.auxiliary.v,
                                                                 'competing_nucleotides': split.auxiliary.competing_nucleotides}},
                                                                 split_summary_path)
                summary_index[split.name] = split_summary_path
                counter += 1

        self.progress.end()
        self.run.info('profile_summary_dir', summary_dir)
        dictio.write_serialized_object(dictio.strip_prefix_from_dict_values(summary_index, self.output_directory), summary_index_output_path)
        self.run.info('profile_summary_index', summary_index_output_path)


    def check_contigs(self):
        if not len(self.contigs):
            raise ConfigError, "0 contigs to work with. Bye."


    def cluster_contigs(self):
        clusterings = []

        for config_name in self.clustering_configs:
            config_path = self.clustering_configs[config_name]

            config = ClusteringConfiguration(config_path, self.output_directory, db_paths = self.database_paths, row_ids_of_interest = self.split_names)

            try:
                newick = clustering.order_contigs_simple(config, progress = self.progress)
            except Exception as e:
                self.run.warning('Clustering has failed for "%s": "%s"' % (config_name, e))
                self.progress.end()
                continue

            clusterings.append(config_name)
            db_entries = tuple([config_name, newick])

            profile_db = dbops.ProfileDatabase(self.profile_db_path, quiet=True)
            profile_db.db._exec('''INSERT INTO %s VALUES (?,?)''' % t.clusterings_table_name, db_entries)
            profile_db.disconnect()

        self.run.info('default_clustering', constants.single_default)
        self.run.info('available_clusterings', clusterings)

        profile_db = dbops.ProfileDatabase(self.profile_db_path, quiet=True)
        profile_db.db.set_meta_value('default_clustering', constants.single_default)
        profile_db.db.set_meta_value('available_clusterings', ','.join(clusterings))
        profile_db.disconnect()


    def check_args(self):
        if (not self.input_file_path) and (not self.serialized_profile_path):
            raise ConfigError, "You must declare either an input file, or a serialized profile. Use '--help'\
                                      to learn more about the command line parameters."
        if self.input_file_path and self.serialized_profile_path:
            raise ConfigError, "You can't declare both an input file and a serialized profile."
        if self.serialized_profile_path and (not self.output_directory):
            raise ConfigError, "When loading serialized profiles, you need to declare an output directory."
        if self.input_file_path and not os.path.exists(self.input_file_path):
            raise ConfigError, "No such file: '%s'" % self.input_file_path
        if self.serialized_profile_path and not os.path.exists(self.serialized_profile_path):
            raise ConfigError, "No such file: '%s'" % self.serialized_profile_path
        if not self.min_coverage_for_variability >= 0:
            raise ConfigError, "Minimum coverage for variability must be 0 or larger."
        if not self.min_mean_coverage >= 0:
            raise ConfigError, "Minimum mean coverage must be 0 or larger."
        if not self.min_contig_length >= 0:
            raise ConfigError, "Minimum contig length must be 0 or larger."
