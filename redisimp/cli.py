# std lib
import argparse
import sys
import time
import logging
from signal import signal, SIGTERM

# 3rd party
import rediscluster
import redis
import redislite
from redis.exceptions import BusyLoadingError

# internal
from .multi import multi_copy

__all__ = ['main']

# how long to wait in between each try
REDISLITE_LOAD_WAIT_INTERVAL_SECS = 1

# how many seconds total to wait before giving up on redislite rdb loading
REDISLITE_LOAD_WAIT_TIMEOUT = 10000


def parse_args():
    """
    parse the cli args and print out help if needed.
    :return: argparse.Namespace
    """
    parser = argparse.ArgumentParser(
        description='import data from redis shards into current redis server')
    parser.add_argument(
        '-s', '--src', type=str, required=True,
        help='comma separated list of hosts in the form of hostname:port')

    parser.add_argument(
        '-d', '--dst', type=str, required=True,
        help='the destination in the form of hostname:port')

    parser.add_argument(
        '-w', '--workers', type=int, default=None,
        help='the number of workers to run in parallel.')

    parser.add_argument(
        '-f', '--filter', type=str, default=None,
        help='a glob-style matching filter to select the keys to copy')

    parser.add_argument(
        '-v', '--verbose', type=bool, default=False,
        help='turn on verbose output')

    return parser.parse_args()


def resolve_host(target):
    """
    :param target: str The host:port pair or path
    :return:
    """
    target = target.strip()
    if target.startswith('redis://') or target.startswith('unix://'):
        return redis.StrictRedis.from_url(target)

    try:
        hostname, port = target.split(':')
        return redis.StrictRedis(host=hostname, port=int(port))
    except ValueError:
        start = time.time()
        while True:
            try:
                redislite.StrictRedis.start_timeout = REDISLITE_LOAD_WAIT_TIMEOUT
                conn = redislite.StrictRedis(target)
            except BusyLoadingError:
                logging.info('%s loading', target)
                elapsed = time.time() - start
                if elapsed > REDISLITE_LOAD_WAIT_TIMEOUT:
                    raise BusyLoadingError('unable to load rdb %s' % target)
                time.sleep(REDISLITE_LOAD_WAIT_INTERVAL_SECS)
                continue

            if conn.info('persistence').get('loading', 0):
                logging.warn('%s loading', target)
                time.sleep(REDISLITE_LOAD_WAIT_INTERVAL_SECS)
                elapsed = time.time() - start
                if elapsed > REDISLITE_LOAD_WAIT_TIMEOUT:
                    raise BusyLoadingError('unable to load rdb %s' % target)
                continue
            return conn


def resolve_sources(srcstring):
    for hoststring in srcstring.split(','):
        hoststring = hoststring.strip()
        if len(hoststring) < 1:
            continue
        yield resolve_host(hoststring)


def resolve_destination(dststring):
    conn = resolve_host(dststring)
    if not conn.info('cluster').get('cluster_enabled', None):
        return conn

    host, port = dststring.split(':')
    return rediscluster.StrictRedisCluster(startup_nodes=[{'host': host, 'port': port}])


def sigterm_handler(signum, frame):
    # pylint: disable=unused-argument
    raise SystemExit('--- Caught SIGTERM; Attempting to quit gracefully ---')


def process(src, dst, verbose=False, worker_count=None, match=None):
    dst = resolve_destination(dst)
    processed = 0
    src_list = [s for s in resolve_sources(src)]

    for key in multi_copy(src_list, dst, worker_count=worker_count, match=match):
        processed += 1
        if verbose:
            print key

        if not verbose and processed % 1000 == 0:
            sys.stdout.write('\r%d' % processed)
            sys.stdout.flush()

    print ""
    print "processed %s keys" % processed


def main():
    signal(SIGTERM, sigterm_handler)
    args = parse_args()
    process(src=args.src, dst=args.dst,
            verbose=args.verbose, worker_count=args.workers, match=args.match)
