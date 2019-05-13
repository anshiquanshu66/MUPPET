#!/usr/bin/env python3
# Copyright 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
"""Rank documents with TF-IDF scores"""

import logging
import numpy as np
import scipy.sparse as sp

from multiprocessing.pool import ThreadPool
from functools import partial
from sklearn.preprocessing import normalize

from hotpot.tokenizers.tokenizer import ngrams_from_tokens
from . import utils
from hotpot.tokenizers.simple_tokenizer import SimpleTokenizer
from hotpot.tokenizers.spacy_tokenizer import SpacyTokenizer
from hotpot.tokenizers.corenlp_tokenizer import CoreNLPTokenizer
from hotpot import tokenizers

logger = logging.getLogger(__name__)
from hotpot import config


class TfidfDocRanker(object):
    """Loads a pre-weighted inverted index of token/document terms.
    Scores new queries by taking sparse dot products.
    """

    def __init__(self, tfidf_path=None, strict=True, normalize_vectors=False, tokenizer='corenlp'):
        """
        Args:
            tfidf_path: path to saved model file
            strict: fail on empty queries or continue (and return empty result)
        """
        # Load from disk
        tfidf_path = tfidf_path or config.TFIDF_FILE
        logger.info('Loading %s' % tfidf_path)
        matrix, metadata = utils.load_sparse_csr(tfidf_path)
        self.doc_mat = matrix if not normalize_vectors else normalize(matrix, axis=0).tocsr()
        self.ngrams = metadata['ngram']
        self.hash_size = metadata['hash_size']
        self.tokenizer = tokenizers.get_class(tokenizer)()
        self.tokenizer_name = tokenizer
        self.doc_freqs = metadata['doc_freqs'].squeeze()
        self.doc_dict = metadata['doc_dict']
        self.num_docs = len(self.doc_dict[0])
        self.strict = strict

    def get_doc_index(self, doc_id):
        """Convert doc_id --> doc_index"""
        return self.doc_dict[0][doc_id]

    def get_doc_id(self, doc_index):
        """Convert doc_index --> doc_id"""
        return self.doc_dict[1][doc_index]

    def get_similarity_with_doc(self, query, doc_id):
        spvec = self.text2spvec(query)
        doc_index = self.get_doc_index(doc_id)
        score = (spvec * self.doc_mat).getcol(doc_index).data
        return 0 if len(score) == 0 else score[0]

    def closest_docs(self, query, k=1, tokenized=False):
        """Closest docs by dot product between query and documents
        in tfidf weighted word vector space.
        """
        spvec = self.text2spvec(query, tokenized)
        res = spvec * self.doc_mat

        if len(res.data) <= k:
            o_sort = np.argsort(-res.data)
        else:
            o = np.argpartition(-res.data, k)[0:k]
            o_sort = o[np.argsort(-res.data[o])]

        doc_scores = res.data[o_sort]
        doc_ids = [self.get_doc_id(i) for i in res.indices[o_sort]]
        return doc_ids, doc_scores

    def batch_closest_docs(self, queries, k=1, num_workers=None, tokenized=False):
        if self.tokenizer_name == 'corenlp' and num_workers > 1:  # FIXME: fix corenlp to support this!
            raise ValueError("corenlp not supporting multiprocessing yet")
        """Process a batch of closest_docs requests multithreaded.
        Note: we can use plain threads here as scipy is outside of the GIL.
        """
        with ThreadPool(num_workers) as threads:
            closest_docs = partial(self.closest_docs, k=k, tokenized=tokenized)
            results = threads.map(closest_docs, queries)
        return results

    def parse(self, query, tokenized=False):
        """Parse the query into tokens (either ngrams or tokens)."""
        if not tokenized:
            tokens = self.tokenizer.tokenize(query)
            return tokens.ngrams(n=self.ngrams, uncased=True,
                                 filter_fn=utils.filter_ngram)
        else:
            return ngrams_from_tokens(query, n=self.ngrams, uncased=True,
                                      filter_fn=utils.filter_ngram)

    def text2spvec(self, query, tokenized=False):
        """Create a sparse tfidf-weighted word vector from query.

        tfidf = log(tf + 1) * log((N - Nt + 0.5) / (Nt + 0.5))
        """
        # Get hashed ngrams
        if tokenized:
            parsed = [utils.normalize(x) for x in query]
        else:
            parsed = utils.normalize(query)
        words = self.parse(parsed, tokenized)
        wids = [utils.hash(w, self.hash_size) for w in words]

        if len(wids) == 0:
            if self.strict:
                raise RuntimeError('No valid word in: %s' % query)
            else:
                logger.warning('No valid word in: %s' % query)
                return sp.csr_matrix((1, self.hash_size))

        # Count TF
        wids_unique, wids_counts = np.unique(wids, return_counts=True)
        tfs = np.log1p(wids_counts)

        # Count IDF
        Ns = self.doc_freqs[wids_unique]
        idfs = np.log((self.num_docs - Ns + 0.5) / (Ns + 0.5))
        idfs[idfs < 0] = 0

        # TF-IDF
        data = np.multiply(tfs, idfs)

        # One row, sparse csr matrix
        indptr = np.array([0, len(wids_unique)])
        spvec = sp.csr_matrix(
            (data, wids_unique, indptr), shape=(1, self.hash_size)
        )

        return spvec
