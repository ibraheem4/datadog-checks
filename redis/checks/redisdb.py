'''
Redis checks
'''
import re
import time
from checks import AgentCheck

class Redis(AgentCheck):
    db_key_pattern = re.compile(r'^db\d+')
    subkeys = ['keys', 'expires']
    GAUGE_KEYS = {
        # Append-only metrics
        'aof_last_rewrite_time_sec':    'redis.aof.last_rewrite_time',
        'aof_rewrite_in_progress':      'redis.aof.rewrite',
        'aof_current_size':             'redis.aof.size',
        'aof_buffer_length':            'redis.aof.buffer_length',

        # Network
        'connected_clients':            'redis.net.clients',
        'connected_slaves':             'redis.net.slaves',
        'rejected_connections':         'redis.net.rejected',

        # clients
        'blocked_clients':              'redis.clients.blocked',
        'client_biggest_input_buf':     'redis.clients.biggest_input_buf',
        'client_longest_output_list':   'redis.clients.longest_output_list',

        # Keys
        'evicted_keys':                 'redis.keys.evicted',
        'expired_keys':                 'redis.keys.expired',

        # stats
        'keyspace_hits':                'redis.stats.keyspace_hits',
        'keyspace_misses':              'redis.stats.keyspace_misses',
        'latest_fork_usec':             'redis.perf.latest_fork_usec',

        # pubsub
        'pubsub_channels':              'redis.pubsub.channels',
        'pubsub_patterns':              'redis.pubsub.patterns',

        # rdb
        'rdb_bgsave_in_progress':       'redis.rdb.bgsave',
        'rdb_changes_since_last_save':  'redis.rdb.changes_since_last',
        'rdb_last_bgsave_time_sec':     'redis.rdb.last_bgsave_time',

        # memory
        'mem_fragmentation_ratio':      'redis.mem.fragmentation_ratio',
        'used_memory':                  'redis.mem.used',
        'used_memory_lua':              'redis.mem.lua',
        'used_memory_peak':             'redis.mem.peak',
        'used_memory_rss':              'redis.mem.rss',

        # replication
        'master_last_io_seconds_ago':   'redis.replication.last_io_seconds_ago',
        'master_sync_in_progress':      'redis.replication.sync',
        'master_sync_left_bytes':       'redis.replication.sync_left_bytes',

    }

    RATE_KEYS = {
        # cpu
        'used_cpu_sys':                 'redis.cpu.sys',
        'used_cpu_sys_children':        'redis.cpu.sys_children',
        'used_cpu_user':                'redis.cpu.user',
        'used_cpu_user_children':       'redis.cpu.user_children',
    }

    RATIO_KEYS = {
        'hit_ratio': ['keyspace_hits', 'keyspace_misses'],
    }

    def __init__(self, name, init_config, agentConfig):
        AgentCheck.__init__(self, name, init_config, agentConfig)

        try:
            import redis
        except ImportError:
            self.log.error('redisdb.yaml exists but redis module can not be imported. Skipping check.')

        self.previous_total_commands = {}
        self.connections = {}

    def _parse_dict_string(self, string, key, default):
        """Take from a more recent redis.py, parse_info"""
        try:
            for item in string.split(','):
                k, v = item.rsplit('=', 1)
                if k == key:
                    try:
                        return int(v)
                    except ValueError:
                        return v
            return default
        except Exception, e:
            self.log.exception("Cannot parse dictionary string: %s" % string)
            return default

    def _get_conn(self, host, port, password, db):
        import redis
        key = (host, port, db)
        if key not in self.connections:
            if password is not None and len(password) > 0:
                try:
                    self.connections[key] = redis.Redis(host=host, port=port, password=password, db=db)
                except TypeError:
                    self.log.exception("You need a redis library that supports authenticated connections. Try easy_install redis.")
                    raise
            else:
                self.connections[key] = redis.Redis(host=host, port=port, db=db)

        return self.connections[key]

    def _check_db(self, host, port, password, db, list_lengths,  custom_tags=None):
        conn = self._get_conn(host, port, password, db)
        tags = set(custom_tags or [])
        tags = sorted(tags.union(["redis_host:%s" % host,
                                  "redis_port:%s" % port]))
      
        # Ping the database for info, and track the latency.
        start = time.time()
        info = conn.info()
        latency_ms = round((time.time() - start) * 1000, 2)
        self.gauge('redis.info.latency_ms', latency_ms, tags=tags)

        # Save the database statistics.
        for key in info.keys():
            if self.db_key_pattern.match(key):
                db_tags = list(tags) + ["redis_db:" + key]
                for subkey in self.subkeys:
                    # Old redis module on ubuntu 10.04 (python-redis 0.6.1) does not
                    # returns a dict for those key but a string: keys=3,expires=0
                    # Try to parse it (see lighthouse #46)
                    val = -1
                    try:
                        val = info[key].get(subkey, -1)
                    except AttributeError:
                        val = self._parse_dict_string(info[key], subkey, -1)
                    metric = '.'.join(['redis', subkey])
                    self.gauge(metric, val, tags=db_tags)
                # Try and calculate a ratio of expiring:non-expiring keys
                try:
                    self.gauge('redis.expires_ratio',
                        round(100 * (float(info[key]['expires']) / float(info[key]['keys'])), 2),
                        tags=db_tags)
                except AttributeError:
                    pass

        # Save a subset of db-wide statistics
        [self.gauge(self.GAUGE_KEYS[k], info[k], tags=tags) for k in self.GAUGE_KEYS if k in info]
        [self.rate (self.RATE_KEYS[k],  info[k], tags=tags) for k in self.RATE_KEYS  if k in info]

        # Calculate ratios, e.g. hit/miss ratio
        for name, values in self.RATIO_KEYS.items():
            val1 = info[values[0]]
            val2 = info[values[1]]
            if 0 not in [val1, val2]:
                self.gauge('redis.%s' % name,
                    round(100 * (float(val1) / float(val2)), 2),
                    tags=tags)

        # Calculate the length of lists specified in list_lengths
        from redis.exceptions import ResponseError
        for _list in list_lengths:
            try:
                self.gauge('redis.llen.%s' % _list, conn.llen(_list), tags=tags)
            except ResponseError, e:
                self.log.error("Could not get length of %s: %s " % (key, e))
                pass

        # Save the number of commands.
        total_commands = info['total_commands_processed'] - 1
        tuple_tags = tuple(tags)
        if tuple_tags in self.previous_total_commands:
            count = total_commands - self.previous_total_commands[tuple_tags]
            self.gauge('redis.net.commands', count, tags=tags)
        self.previous_total_commands[tuple_tags] = total_commands

    def check(self, instance):
        # Allow the default redis database to be overridden.
        host = instance.get('host', 'localhost')
        port = instance.get('port', 6379)
        db = instance.get('db', 0) # DB on which to run llen checks
        password = instance.get('password', None)
        custom_tags = instance.get('tags', [])
        list_lengths = instance.get('list_lengths', [])

        self._check_db(host, int(port), password, int(db), list_lengths, custom_tags)


if __name__ == '__main__':
    from pprint import pprint
    check, instances = Redis.from_yaml('/home/mike/dd-agent/conf.d/redis2.yaml')
    for instance in instances:
        print "\nRunning the check against url: %s" % (instance['host'])
        check.check(instance)
        if check.has_events():
            print 'Events: %s' % (pprint(check.get_events()))
        print 'Metrics: %s' % (pprint(check.get_metrics()))


