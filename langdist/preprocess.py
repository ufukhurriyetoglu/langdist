# -*- coding: UTF-8 -*-
"""
This module is used to preprocess corpora.
"""
import os
import pickle

import regex

from langdist import PACKAGE_ROOT
from langdist.util import CorpusParser

__author__ = 'kensk8er1017@gmail.com'

_PROCESSED_CORPUS_DIR = os.path.join(PACKAGE_ROOT, os.path.pardir, 'corpora')


def _preprocess(paragraph, locale):
    """Preprocess a paragraph."""
    paragraph = paragraph.strip()

    # for some reason, zh text has white spaces between characters, which isn't normal for zh texts
    if locale == 'zh':
        paragraph = regex.sub(r'\s', '', paragraph)

    return paragraph


def preprocess_corpus(locale):
    """
    Preprocess the corpus of the specified locale and store it into a pickle file.

    :param locale: locale of the corpus to preprocess
    """
    corpus = list()
    parser = CorpusParser(locale)
    for paragraph in parser.gen_paragraphs():
        paragraph = _preprocess(paragraph, locale)
        corpus.append(paragraph)

    processed_filepath = os.path.join(_PROCESSED_CORPUS_DIR, '{}.pkl'.format(locale))
    with open(processed_filepath, 'wb') as processed_file:
        pickle.dump(corpus, processed_file)


if __name__ == '__main__':
    preprocess_corpus('en')
