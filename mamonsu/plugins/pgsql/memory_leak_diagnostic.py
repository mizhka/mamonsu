from mamonsu.plugins.pgsql.plugin import PgsqlPlugin as Plugin
import os
from .pool import Pooler
import re
from distutils.version import LooseVersion
import mamonsu.lib.platform as platform
from mamonsu.lib.plugin import PluginDisableException
import logging

class MemoryLeakDiagnostic(Plugin):
    DEFAULT_CONFIG = {'enabled': 'False',
                      'private_anon_mem_threshold': '1GB'}
    Interval = 60

    query = 'select pid from pg_stat_activity'
    key_count_diff = 'pgsql.memory_leak_diagnostic.count_diff[]'
    key_count_diff_error = 'pgsql.memory_leak_diagnostic.msg_text[]'
    name_count_diff = 'PostgreSQL: number of pids which private anonymous memory exceeds ' \
                      'private_anon_mem_threshold'
    name_count_diff_error = 'PostgreSQL: number of pids which private anonymous memory ' \
                            'exceeds private_anon_mem_threshold, text of message'

    def __init__(self, config):
        super(Plugin, self).__init__(config)
        if not platform.LINUX:
            self.disable()
            logging.error('Plugin {name} work only on Linux. '.format(name=self.__class__.__name__))

        if self.is_enabled():
            self.page_size = os.sysconf('SC_PAGE_SIZE')

            private_anon_mem_threshold_row = self.plugin_config('private_anon_mem_threshold').upper()
            private_anon_mem_threshold, prefix = re.match(r'([0-9]*)([A-Z]*)',
                                                          private_anon_mem_threshold_row, re.I).groups()
            ratio = 0

            if prefix == 'MB':
                ratio = 1024 * 1024
            elif prefix == 'GB':
                ratio = 1024 * 1024 * 1024
            elif prefix == 'TB':
                ratio = 1024 * 1024 * 1024 * 1024
            else:
                self.disable()
                logging.error('Error in config, section [{section}], parameter private_anon_mem_threshold. '
                              'Possible values MB, GB, TB. For example 1GB.'
                              .format(section=self.__class__.__name__.lower()))
                ratio = 1024 * 1024 * 1024

            self.diff = ratio * int(private_anon_mem_threshold)

            self.os_release = os.uname().release
            os_release_file = '/etc/os-release'
            try:
                release_file = open(os_release_file, 'r').readlines()
            except Exception as e:
                self.disable()
                release_file = None
                logging.error(f'Cannot read file {os_release_file} : {e}')

            if release_file:
                for line in release_file:
                    if line.strip('"\n') != '':
                        k, v = line.split('=', 1)
                        if k == 'ID':
                            self.os_name = v.strip('"\n')
                        elif k == 'VERSION_ID':
                            self.os_version = v.strip('"\n')

    def run(self, zbx):
        pids = []
        count_diff = 0
        diffs = []
        msg_text = ''

        for row in Pooler.query(query=self.query):
            pids.append(row[0])
        print(self.os_release.split('.')[0])
        print(int(self.os_release.split('.')[1]))
        print(self.os_name)
        print(self.os_version)
        if LooseVersion(self.os_release) < LooseVersion("4.5") and \
                not (self.os_name == 'centos' and self.os_version == '7'):
            print('point 1')
            for pid in pids:
                try:
                    statm = open(f'/proc/{pid}/statm', 'r').read().split(' ')
                except FileNotFoundError:
                    continue

                RES = int(statm[1]) * self.page_size
                SHR = int(statm[2]) * self.page_size
                if RES - SHR > self.diff:
                    count_diff += 1
                    diffs.append({'pid': pid, 'RES': RES, 'SHR': SHR, 'diff': self.diff})
            if diffs:
                for diff in diffs:
                    msg_text += 'pid: {pid},  RES {RES} - SHR {SHR} more then {diff}\n'.format_map(diff)
        else:
            print('point 2')
            for pid in pids:
                try:
                    statm = open(f'/proc/{pid}/status', 'r').readlines()
                except FileNotFoundError:
                    continue

                for line in statm:
                    VmRSS = 0
                    RssAnon = 0
                    RssFile = 0
                    RssShmem = 0
                    k, v = line.split(':\t', 1)

                    if k == 'VmRSS':
                        VmRSS = int(v.strip('"\n\t ').split(' ')[0]) * 1024
                    elif k == 'RssAnon':
                        RssAnon = int(v.strip('"\n\t ').split(' ')[0]) * 1024
                    elif k == 'RssFile':
                        RssFile = int(v.strip('"\n\t ').split(' ')[0]) * 1024
                    elif k == 'RssShmem':
                        RssShmem = int(v.strip('"\n\t ').split(' ')[0]) * 1024
                    if RssAnon > self.diff:
                        count_diff += 1
                        diffs.append(
                            {'pid': pid, 'VmRSS': VmRSS, 'RssAnon': RssAnon, 'RssFile': RssFile, 'RssShmem': RssShmem,
                             'diff': self.diff})
            if diffs:
                for diff in diffs:
                    msg_text += 'pid: {pid},  RssAnon {RssAnon} more then {diff}, VmRSS {VmRSS}, ' \
                                       'RssFile {RssFile}, RssShmem {RssShmem} \n'.format_map(diff)

        zbx.send(self.key_count_diff, int(count_diff))
        zbx.send(self.key_count_diff_error, msg_text)

    def items(self, template):
        result = template.item(
            {
                'name': self.name_count_diff,
                'key': self.key_count_diff,
                'delay': self.plugin_config('interval')
            }
        )
        result += template.item(
            {
                'name': self.name_count_diff_error,
                'key': self.key_count_diff_error,
                'delay': self.plugin_config('interval'),
                'value_type': Plugin.VALUE_TYPE.text
            }
        )
        return result

    def graphs(self, template):
        result = template.graph(
            {
                'name': self.name_count_diff,
                'items': [
                    {
                        'key': self.key_count_diff,
                        'color': 'EEEEEE'
                    }
                ]
            }
        )
        return result

    def triggers(self, template):
        result = template.trigger(
            {
                'name': self.name_count_diff + ' on {HOSTNAME}. {ITEM.LASTVALUE}',
                'expression': '{{#TEMPLATE:{name}.strlen()'
                              '}}&gt;1'.format(name=self.key_count_diff_error)
            })
        return result
