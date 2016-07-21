import re

__all__ = ['copy']


def read_keys(src, batch_size=500, pattern=None):
    """
    iterate through batches of keys from source
    :param src: redis.StrictRedis
    :param batch_size: int
    :param pattern: str
    :yeild: array of keys
    :return:
    """
    if pattern is not None and pattern.startswith('/') and pattern.endswith('/'):
        regex_pattern = re.compile(pattern[1:-1])
        pattern = None
    else:
        regex_pattern = None

    cursor = 0
    while True:
        cursor, keys = src.scan(cursor=cursor, count=batch_size, match=pattern)
        if keys:
            if regex_pattern is not None:
                keys = [key for key in keys if regex_pattern.match(key)]
            yield keys

        if cursor == 0:
            break


def read_data_and_pttl(src, keys):
    pipe = src.pipeline(transaction=False)
    for key in keys:
        pipe.dump(key)
        pipe.pttl(key)
    res = pipe.execute()

    for i, key in enumerate(keys):
        ii = i * 2
        data = res[ii]
        pttl = int(res[ii + 1])
        if len(data) < 1:
            continue
        if pttl < 1:
            pttl = 0
        yield key, data, pttl


def compare_version(version1, version2):
    def normalize(v):
        return [int(x) for x in re.sub(r'(\.0+)*$', '', v).split(".")]

    return cmp(normalize(version1), normalize(version2))


def _supports_replace(conn):
    version = conn.info().get('redis_version')
    if not version:
        return False

    if compare_version(version, '3.0.0') >= 0:
        return True
    else:
        return False


def _replace_restore(pipe, key, pttl, data):
    pipe.execute_command('RESTORE', key, pttl, data, 'REPLACE')


def _delete_restore(pipe, key, pttl, data):
    pipe.delete(key)
    pipe.restore(key, pttl, data)


def _get_restore_handler(conn):
    if _supports_replace(conn):
        return _replace_restore
    else:
        return _delete_restore


def copy(src, dst, pattern=None, backfill=False):
    if backfill:
        return backfill_copy(src=src, dst=dst, pattern=pattern)
    else:
        return clobber_copy(src=src, dst=dst, pattern=pattern)


def clobber_copy(src, dst, pattern=None):
    """
    yields the keys it processes as it goes.
    :param pattern:
    :param src: redis.StrictRedis
    :param dst: redis.StrictRedis or rediscluster.StrictRedisCluster
    :return: None
    """
    read = read_data_and_pttl
    _restore = _get_restore_handler(dst)

    for keys in read_keys(src, pattern=pattern):
        pipe = dst.pipeline(transaction=False)
        for key, data, pttl in read(src, keys):
            _restore(pipe, key, pttl, data)
            yield key
        pipe.execute()


def backfill_copy(src, dst, pattern=None):
    """
    yields the keys it processes as it goes.
    WON'T OVERWRITE the key if it exists. It'll skip over it.
    :param src: redis.StrictRedis
    :param dst: redis.StrictRedis or rediscluster.StrictRedisCluster
    :param pattern: str
    :return: None
    """
    read = read_data_and_pttl
    for keys in read_keys(src, pattern=pattern):
        # don't even bother reading the data if the key already exists in the src.
        pipe = dst.pipeline(transaction=False)
        for key in keys:
            pipe.exists(key)
        keys = [keys[i] for i, result in enumerate(pipe.execute()) if
                not result]
        if not keys:
            continue

        pipe = dst.pipeline(transaction=False)

        for key, data, pttl in read(src, keys):
            pipe.restore(key, pttl, data)

        for i, result in enumerate(pipe.execute(raise_on_error=False)):
            if not isinstance(result, Exception):
                yield keys[i]
                continue

            if 'is busy' in str(result):
                continue

            raise result
