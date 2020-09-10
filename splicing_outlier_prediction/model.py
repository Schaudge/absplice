import itertools
from tqdm import tqdm
import pandas as pd
from kipoi.data import SampleIterator
from mmsplice import MMSplice
from mmsplice.junction_dataloader import JunctionPSI5VCFDataloader, \
    JunctionPSI3VCFDataloader
from mmsplice.utils import encodeDNA, df_batch_writer, \
    delta_logit_PSI_to_delta_PSI
from splicing_outlier_prediction.utils import get_abs_max_rows
from splicing_outlier_prediction.dataloader import RefTableMixin


class SpliceOutlierDataloader(RefTableMixin, SampleIterator):

    def __init__(self, fasta_file, vcf_file, ref_table5=None, ref_table3=None, **kwargs):
        super().__init__(ref_table5, ref_table3, **kwargs)
        self.fasta_file = fasta_file
        self.vcf_file = vcf_file
        self._generator = iter([])

        if self.ref_table5:
            self.dl5 = JunctionPSI5VCFDataloader(
                ref_table5, fasta_file, vcf_file, encode=False, **kwargs)
            self._generator = itertools.chain(
                self._generator,
                self._iter_dl(self.dl5, self.ref_table5, event_type='psi5'))

        if self.ref_table3:
            self.dl3 = JunctionPSI3VCFDataloader(
                ref_table3, fasta_file, vcf_file, encode=False, **kwargs)
            self._generator = itertools.chain(
                self._generator,
                self._iter_dl(self.dl3, self.ref_table3, event_type='psi3'))

    def _iter_dl(self, dl, ref_table, event_type):
        for row in dl:
            junction_id = row['metadata']['exon']['junction']
            ref_row = ref_table.df.loc[junction_id]
            row['metadata']['junction'] = dict()
            row['metadata']['junction']['junction'] = ref_row.name
            row['metadata']['junction']['event_type'] = event_type
            row['metadata']['junction'].update(ref_row.to_dict())
            yield row

    def __next__(self):
        return next(self._generator)

    def __iter__(self):
        return self

    def batch_iter(self, batch_size=32, **kwargs):
        for batch in super().batch_iter(batch_size, **kwargs):
            batch['inputs']['seq'] = self._encode_batch_seq(
                batch['inputs']['seq'])
            batch['inputs']['mut_seq'] = self._encode_batch_seq(
                batch['inputs']['mut_seq'])
            yield batch

    def _encode_batch_seq(self, batch):
        return {k: encodeDNA(v.tolist()) for k, v in batch.items()}


class SpliceOutlier:

    def __init__(self, clip_threshold=None):
        self.mmsplice = MMSplice()
        self.clip_threshold = clip_threshold

    def predict_on_batch(self, batch):
        columns = [
            'samples', 'maf',
            *batch['metadata']['junction'].keys()
        ]
        df = self.mmsplice._predict_batch(batch, columns)
        del df['exons']
        df = df.rename(columns={'ID': 'variant'})
        delta_psi = delta_logit_PSI_to_delta_PSI(
            df['delta_logit_psi'],
            df['ref_psi'],
            clip_threshold=self.clip_threshold or 0.01
        )
        df.insert(18, 'delta_psi', delta_psi)
        return df

    def _predict_on_dataloader(self, dataloader,
                               batch_size=512, progress=True):
        dt_iter = dataloader.batch_iter(batch_size=batch_size)
        if progress:
            dt_iter = tqdm(dt_iter)

        for batch in dt_iter:
            yield self.predict_on_batch(batch)

    def predict_on_dataloader(self, dataloader, batch_size=512, progress=True):
        return SplicingOutlierResult(pd.concat(
            self._predict_on_dataloader(
                dataloader,
                batch_size=batch_size,
                progress=progress)
        ))

    def predict_save(self, dataloader, output_csv,
                     batch_size=512, progress=True):
        df_batch_writer(self._predict_on_dataloader(dataloader), output_csv)


class SplicingOutlierResult:

    def __init__(self, df):
        self.df = df
        self._junction = None
        self._splice_site = None
        self._gene = None

    @classmethod
    def read_csv(cls, path, **kwargs):
        return cls(pd.read_csv(path, **kwargs))

    @staticmethod
    def _explode_samples(df):
        df = df.copy()
        df['samples'] = df['samples'].str.split(';')
        return df.rename(columns={'samples': 'sample'}).explode('sample')

    @property
    def junction(self):
        if self._junction is None:
            df = self.df
            if 'samples' in self.df:
                df = self._explode_samples(df)
                index = ['junction', 'sample']
            else:
                index = 'junction'
            self._junction = df.set_index(index)
        return self._junction

    @property
    def splice_site(self):
        if self._splice_site is None:
            index = ['splice_site', 'sample'] \
                if 'samples' in self.df else 'splice_site'
            self._splice_site = get_abs_max_rows(
                self.junction, index, 'delta_psi')
        return self._splice_site

    @property
    def gene(self):
        if self._gene is None:
            index = ['gene_id', 'sample'] \
                if 'samples' in self.df else 'gene_id'
            self._gene = get_abs_max_rows(
                self.junction, index, 'delta_psi')
        return self._gene

    def infer_cat(self, cat_inference, progress=False):
        if 'samples' not in self.df.columns:
            raise ValueError('"samples" column is missing.')

        rows = self.junction.iterrows()
        if progress:
            rows = tqdm(rows, total=self.junction.shape[0])

        df = pd.DataFrame([
            cat_inference.infer(junction, sample, row['event_type'])
            for (junction, sample), row in rows
            if cat_inference.contains(junction, sample, row['event_type'])
        ]).set_index(['junction', 'sample'])
        self._junction = self.junction.join(df)
        del self._splice_site
        del self._gene

    def add_maf(self, population):
        self.df['maf'] = self.df['variant'].map(lambda x: population.get(x, 0))

    def filter_maf(self, max_num_sample=2, population=None, maf_cutoff=0.001):
        df = self.df[self.df.str.split(';').map(len) <= max_num_sample]

        if population:
            df = df['variant'].map(
                lambda x: population.get(x, 0) <= maf_cutoff)

        return SplicingOutlierResult(df)
