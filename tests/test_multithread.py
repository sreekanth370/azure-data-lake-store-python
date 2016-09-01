# -*- coding: utf-8 -*-
# coding=utf-8
# --------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
# --------------------------------------------------------------------------

from contextlib import contextmanager
import os
import pytest
import shutil
import signal
import tempfile
import threading

from adlfs.core import AzureDLPath
from adlfs.multithread import ADLDownloader, ADLUploader
from tests.testing import azure, azure_teardown, md5sum, my_vcr, posix, working_dir

test_dir = working_dir()


@pytest.yield_fixture()
def tempdir():
    tmpdir = tempfile.mkdtemp()
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, True)


def linecount(infile):
    lines = 0
    with open(infile) as f:
        for line in f:
            lines += 1
    return lines

# TODO : when the uploader is ready, should place file in temp location
# rather than rely on file already in place.


@contextmanager
def setup_tree(azure):
    for directory in ['', 'data/a', 'data/b']:
        azure.mkdir(test_dir / directory)
        for filename in ['x.csv', 'y.csv', 'z.txt']:
            with azure.open(test_dir / directory / filename, 'wb') as f:
                f.write(b'123456')
    try:
        yield
    finally:
        for path in azure.ls(test_dir):
            if azure.exists(path):
                azure.rm(path, recursive=True)


def create_remote_csv(fs, name, columns, colwidth, lines):
    from hashlib import md5
    from itertools import cycle, islice
    hashobj = md5()
    haystack = '0123456789ABCDEF'
    row = ','.join([ch * colwidth for ch in islice(cycle(haystack), columns)]) + '\n'
    row = row.encode('utf-8')
    fsize = 0
    with fs.open(name, 'wb') as f:
        for _ in range(0, lines):
            hashobj.update(row)
            f.write(row)
            fsize += len(row)
    return fsize, hashobj.hexdigest()


@my_vcr.use_cassette
def test_download_single_file(tempdir, azure):
    with azure_teardown(azure):
        name = posix(test_dir, 'remote.csv')
        lines = 100
        size, checksum = create_remote_csv(azure, name, 10, 5, lines)
        fname = os.path.join(tempdir, 'local.csv')

        # single chunk
        down = ADLDownloader(azure, name, fname, 1, size + 10)
        assert md5sum(fname) == checksum
        assert os.stat(fname).st_size == size
        assert linecount(fname) == lines
        os.remove(fname)

        # multiple chunks, one thread
        down = ADLDownloader(azure, name, fname, 1, size // 5)
        assert md5sum(fname) == checksum
        assert os.stat(fname).st_size == size
        assert linecount(fname) == lines
        os.remove(fname)


@my_vcr.use_cassette
def test_download_single_to_dir(tempdir, azure):
    with azure_teardown(azure):
        name = posix(test_dir, 'remote.csv')
        lines = 100
        size, checksum = create_remote_csv(azure, name, 10, 5, lines)
        fname = os.path.join(tempdir, 'remote.csv')

        down = ADLDownloader(azure, name, tempdir, 1, 2**24)
        assert md5sum(fname) == checksum
        assert os.stat(fname).st_size == size
        assert linecount(fname) == lines
        os.remove(fname)


@my_vcr.use_cassette
def test_download_many(tempdir, azure):
    with setup_tree(azure):
        down = ADLDownloader(azure, test_dir, tempdir, 1, 2**24)
        nfiles = 0
        for dirpath, dirnames, filenames in os.walk(tempdir):
            nfiles += len(filenames)
        assert nfiles > 1


@my_vcr.use_cassette
def test_download_glob(tempdir, azure):
    with setup_tree(azure):
        remote_path = test_dir / 'data' / 'a' / '*.csv'
        down = ADLDownloader(azure, remote_path, tempdir, run=False)
        assert len(down.rfiles) == 2

        lfiles = [os.path.relpath(f, tempdir) for f in down.lfiles]
        assert lfiles == ['x.csv', 'y.csv']

        remote_path = test_dir / 'data' / '*' / '*.csv'
        down = ADLDownloader(azure, remote_path, tempdir, run=False)
        assert len(down.rfiles) == 4

        lfiles = [os.path.relpath(f, tempdir) for f in down.lfiles]
        assert lfiles == [
            os.path.join('a', 'x.csv'),
            os.path.join('a', 'y.csv'),
            os.path.join('b', 'x.csv'),
            os.path.join('b', 'y.csv')]

        remote_path = test_dir / 'data' / '*' / 'z.txt'
        down = ADLDownloader(azure, remote_path, tempdir, run=False)
        assert len(down.rfiles) == 2

        lfiles = [os.path.relpath(f, tempdir) for f in down.lfiles]
        assert lfiles == [
            os.path.join('a', 'z.txt'),
            os.path.join('b', 'z.txt')]


@my_vcr.use_cassette
def test_save_down(tempdir, azure):
    with setup_tree(azure):
        down = ADLDownloader(azure, test_dir, tempdir, 1, 2**24, run=False)
        down.save()

        alldownloads = ADLDownloader.load()
        assert down.hash in alldownloads

        down.save(keep=False)
        alldownloads = ADLDownloader.load()
        assert down.hash not in alldownloads


@pytest.mark.skipif(True, reason="first assert fails during VCR playback")
def test_interrupt_down(tempdir, azure):
    with setup_tree(azure):
        down = ADLDownloader(azure, test_dir, tempdir, 1, 2**24, run=False)

        def interrupt():
            os.kill(os.getpid(), signal.SIGINT)

        threading.Timer(1, interrupt).start()

        down.run()
        assert down.nchunks > 0

        down.run()
        assert down.nchunks == 0


@pytest.yield_fixture()
def local_files(tempdir):
    filenames = [os.path.join(tempdir, f) for f in ['bigfile', 'littlefile']]
    with open(filenames[0], 'wb') as f:
        for char in b"0 1 2 3 4 5 6 7 8 9".split():
            f.write(char * 1000)
    with open(filenames[1], 'wb') as f:
        f.write(b'0123456789')
    nestpath = os.path.join(tempdir, 'nested1', 'nested2')
    os.makedirs(nestpath)
    for filename in ['a', 'b', 'c']:
        filenames.append(os.path.join(nestpath, filename))
        with open(os.path.join(nestpath, filename), 'wb') as f:
            f.write(b'0123456789')
    yield filenames


@my_vcr.use_cassette
def test_upload_one(local_files, azure):
    with azure_teardown(azure):
        bigfile, littlefile, a, b, c = local_files

        # single chunk
        up = ADLUploader(azure, test_dir / 'littlefile', littlefile, nthreads=1)
        assert azure.info(test_dir / 'littlefile')['length'] == 10

        # multiple chunks, one thread
        size = 10000
        up = ADLUploader(azure, test_dir / 'bigfile', bigfile, nthreads=1,
                         chunksize=size//5, run=False)
        dic = list(up.progress.values())[0]
        dic['uuid'] = 'test'
        dic['files'] = [up.temp_upload_path+"test_%i" % i for i
                        in dic['waiting']]
        up.run()

        assert azure.info(test_dir / 'bigfile')['length'] == size

        azure.rm(test_dir / 'bigfile')


@my_vcr.use_cassette
def test_upload_many(local_files, azure):
    with azure_teardown(azure):
        bigfile, littlefile, a, b, c = local_files
        root = os.path.dirname(bigfile)

        # single thread
        up = ADLUploader(azure, test_dir, root, nthreads=1)
        assert azure.info(test_dir / 'littlefile')['length'] == 10
        assert azure.cat(test_dir / 'nested1/nested2/a') == b'0123456789'
        assert len(azure.du(test_dir, deep=True)) == 5
        assert azure.du(test_dir, deep=True, total=True) == 10000 + 40


@my_vcr.use_cassette
def test_upload_glob(tempdir, azure):
    for directory in ['a', 'b']:
        d = os.path.join(tempdir, 'data', directory)
        os.makedirs(d)
        for data in ['x.csv', 'y.csv', 'z.txt']:
            with open(os.path.join(d, data), 'wb') as f:
                f.write(b'0123456789')

    with azure_teardown(azure):
        local_path = os.path.join(tempdir, 'data', 'a', '*.csv')
        up = ADLUploader(azure, test_dir, local_path, run=False)
        assert len(up.lfiles) == 2

        rfiles = [posix(AzureDLPath(f).relative_to(test_dir))
                  for f in up.rfiles]
        assert rfiles == ['x.csv', 'y.csv']

        local_path = os.path.join(tempdir, 'data', '*', '*.csv')
        up = ADLUploader(azure, test_dir, local_path, run=False)
        assert len(up.lfiles) == 4

        rfiles = [posix(AzureDLPath(f).relative_to(test_dir))
                  for f in up.rfiles]
        assert rfiles == [
            posix('a', 'x.csv'),
            posix('a', 'y.csv'),
            posix('b', 'x.csv'),
            posix('b', 'y.csv')]

        local_path = os.path.join(tempdir, 'data', '*', 'z.txt')
        up = ADLUploader(azure, test_dir, local_path, run=False)
        assert len(up.lfiles) == 2

        rfiles = [posix(AzureDLPath(f).relative_to(test_dir))
                  for f in up.rfiles]
        assert rfiles == [posix('a', 'z.txt'), posix('b', 'z.txt')]


def test_save_up(local_files, azure):
    bigfile, littlefile, a, b, c = local_files
    root = os.path.dirname(bigfile)

    up = ADLUploader(azure, '', root, 1, 1000000, run=False)
    up.save()

    alluploads = ADLUploader.load()
    assert up.hash in alluploads

    up.save(keep=False)
    alluploads = ADLUploader.load()
    assert up.hash not in alluploads


@pytest.mark.skipif(True, reason="first assert fails during VCR playback")
def test_interrupt_up(local_files, azure):
    bigfile, littlefile, a, b, c = local_files
    root = os.path.dirname(bigfile)

    with azure_teardown(azure):
        up = ADLUploader(azure, test_dir, root, 1, 1000000, run=False)

        def interrupt():
            os.kill(os.getpid(), signal.SIGINT)

        threading.Timer(1, interrupt).start()

        up.run()
        assert up.nchunks > 0

        up.run()
        assert up.nchunks == 0
