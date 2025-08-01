#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright (c) 2017, Ansible Project
# GNU General Public License v3.0+ (see LICENSES/GPL-3.0-or-later.txt or https://www.gnu.org/licenses/gpl-3.0.txt)
# SPDX-License-Identifier: GPL-3.0-or-later

# Cronvar Plugin: The goal of this plugin is to provide an idempotent
# method for set cron variable values.  It should play well with the
# existing cron module as well as allow for manually added variables.
# Each variable entered will be preceded with a comment describing the
# variable so that it can be found later.  This is required to be
# present in order for this plugin to find/modify the variable

# This module is based on the crontab module.

from __future__ import absolute_import, division, print_function
__metaclass__ = type

DOCUMENTATION = r"""
module: cronvar
short_description: Manage variables in crontabs
description:
  - Use this module to manage crontab variables.
  - This module allows you to create, update, or delete cron variable definitions.
extends_documentation_fragment:
  - community.general.attributes
attributes:
  check_mode:
    support: none
  diff_mode:
    support: none
options:
  name:
    description:
      - Name of the crontab variable.
    type: str
    required: true
  value:
    description:
      - The value to set this variable to.
      - Required if O(state=present).
    type: str
  insertafter:
    description:
      - If specified, the variable is inserted after the variable specified.
      - Used with O(state=present).
    type: str
  insertbefore:
    description:
      - Used with O(state=present). If specified, the variable is inserted just before the variable specified.
    type: str
  state:
    description:
      - Whether to ensure that the variable is present or absent.
    type: str
    choices: [absent, present]
    default: present
  user:
    description:
      - The specific user whose crontab should be modified.
      - This parameter defaults to V(root) when unset.
    type: str
  cron_file:
    description:
      - If specified, uses this file instead of an individual user's crontab.
      - Without a leading V(/), this is assumed to be in C(/etc/cron.d).
      - With a leading V(/), this is taken as absolute.
    type: str
  backup:
    description:
      - If set, create a backup of the crontab before it is modified. The location of the backup is returned in the C(backup)
        variable by this module.
    type: bool
    default: false
requirements:
  - cron
author:
  - Doug Luce (@dougluce)
"""

EXAMPLES = r"""
- name: Ensure entry like "EMAIL=doug@ansibmod.con.com" exists
  community.general.cronvar:
    name: EMAIL
    value: doug@ansibmod.con.com

- name: Ensure a variable does not exist. This may remove any variable named "LEGACY"
  community.general.cronvar:
    name: LEGACY
    state: absent

- name: Add a variable to a file under /etc/cron.d
  community.general.cronvar:
    name: LOGFILE
    value: /var/log/yum-autoupdate.log
    user: root
    cron_file: ansible_yum-autoupdate
"""

import os
import platform
import pwd
import re
import shlex
import sys
import tempfile

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.six.moves import shlex_quote


class CronVarError(Exception):
    pass


class CronVar(object):
    """
        CronVar object to write variables to crontabs.

        user      - the user of the crontab (defaults to root)
        cron_file - a cron file under /etc/cron.d
    """

    def __init__(self, module, user=None, cron_file=None):
        self.module = module
        self.user = user
        self.lines = None
        self.wordchars = ''.join(chr(x) for x in range(128) if chr(x) not in ('=', "'", '"',))
        self.cron_cmd = self.module.get_bin_path('crontab', required=True)

        if cron_file:
            self.cron_file = ""
            if os.path.isabs(cron_file):
                self.cron_file = cron_file
            else:
                self.cron_file = os.path.join('/etc/cron.d', cron_file)
        else:
            self.cron_file = None

        self.read()

    def read(self):
        # Read in the crontab from the system
        self.lines = []
        if self.cron_file:
            # read the cronfile
            try:
                with open(self.cron_file, 'r') as f:
                    self.lines = f.read().splitlines()
            except IOError:
                # cron file does not exist
                return
            except Exception:
                raise CronVarError("Unexpected error:", sys.exc_info()[0])
        else:
            # using safely quoted shell for now, but this really should be two non-shell calls instead.  FIXME
            (rc, out, err) = self.module.run_command(self._read_user_execute(), use_unsafe_shell=True)

            if rc != 0 and rc != 1:  # 1 can mean that there are no jobs.
                raise CronVarError("Unable to read crontab")

            lines = out.splitlines()
            count = 0
            for l in lines:
                if count > 2 or (not re.match(r'# DO NOT EDIT THIS FILE - edit the master and reinstall.', l
                                              ) and not re.match(r'# \(/tmp/.*installed on.*\)', l) and not re.match(r'# \(.*version.*\)', l)):
                    self.lines.append(l)
                count += 1

    def log_message(self, message):
        self.module.debug('ansible: "%s"' % message)

    def write(self, backup_file=None):
        """
        Write the crontab to the system. Saves all information.
        """
        if backup_file:
            fileh = open(backup_file, 'w')
        elif self.cron_file:
            fileh = open(self.cron_file, 'w')
            path = None
        else:
            filed, path = tempfile.mkstemp(prefix='crontab')
            fileh = os.fdopen(filed, 'w')

        fileh.write(self.render())
        fileh.close()

        # return if making a backup
        if backup_file:
            return

        # Add the entire crontab back to the user crontab
        if not self.cron_file:
            # quoting shell args for now but really this should be two non-shell calls.  FIXME
            (rc, out, err) = self.module.run_command(self._write_execute(path), use_unsafe_shell=True)
            os.unlink(path)

            if rc != 0:
                self.module.fail_json(msg=err)

    def remove_variable_file(self):
        try:
            os.unlink(self.cron_file)
            return True
        except OSError:
            # cron file does not exist
            return False
        except Exception:
            raise CronVarError("Unexpected error:", sys.exc_info()[0])

    def parse_for_var(self, line):
        lexer = shlex.shlex(line)
        lexer.wordchars = self.wordchars
        varname = lexer.get_token()
        is_env_var = lexer.get_token() == '='
        value = ''.join(lexer)
        if is_env_var:
            return (varname, value)
        raise CronVarError("Not a variable.")

    def find_variable(self, name):
        for l in self.lines:
            try:
                (varname, value) = self.parse_for_var(l)
                if varname == name:
                    return value
            except CronVarError:
                pass
        return None

    def get_var_names(self):
        var_names = []
        for l in self.lines:
            try:
                var_name, dummy = self.parse_for_var(l)
                var_names.append(var_name)
            except CronVarError:
                pass
        return var_names

    def add_variable(self, name, value, insertbefore, insertafter):
        if insertbefore is None and insertafter is None:
            # Add the variable to the top of the file.
            self.lines.insert(0, "%s=%s" % (name, value))
        else:
            newlines = []
            for l in self.lines:
                try:
                    varname, dummy = self.parse_for_var(l)  # Throws if not a var line
                    if varname == insertbefore:
                        newlines.append("%s=%s" % (name, value))
                        newlines.append(l)
                    elif varname == insertafter:
                        newlines.append(l)
                        newlines.append("%s=%s" % (name, value))
                    else:
                        raise CronVarError  # Append.
                except CronVarError:
                    newlines.append(l)

            self.lines = newlines

    def remove_variable(self, name):
        self.update_variable(name, None, remove=True)

    def update_variable(self, name, value, remove=False):
        newlines = []
        for l in self.lines:
            try:
                varname, dummy = self.parse_for_var(l)  # Throws if not a var line
                if varname != name:
                    raise CronVarError  # Append.
                if not remove:
                    newlines.append("%s=%s" % (name, value))
            except CronVarError:
                newlines.append(l)

        self.lines = newlines

    def render(self):
        """
        Render a proper crontab
        """
        result = '\n'.join(self.lines)
        if result and result[-1] not in ['\n', '\r']:
            result += '\n'
        return result

    def _read_user_execute(self):
        """
        Returns the command line for reading a crontab
        """
        user = ''

        if self.user:
            if platform.system() == 'SunOS':
                return "su %s -c '%s -l'" % (shlex_quote(self.user), shlex_quote(self.cron_cmd))
            elif platform.system() == 'AIX':
                return "%s -l %s" % (shlex_quote(self.cron_cmd), shlex_quote(self.user))
            elif platform.system() == 'HP-UX':
                return "%s %s %s" % (self.cron_cmd, '-l', shlex_quote(self.user))
            elif pwd.getpwuid(os.getuid())[0] != self.user:
                user = '-u %s' % shlex_quote(self.user)
        return "%s %s %s" % (self.cron_cmd, user, '-l')

    def _write_execute(self, path):
        """
        Return the command line for writing a crontab
        """
        user = ''
        if self.user:
            if platform.system() in ['SunOS', 'HP-UX', 'AIX']:
                return "chown %s %s ; su '%s' -c '%s %s'" % (
                    shlex_quote(self.user), shlex_quote(path), shlex_quote(self.user), self.cron_cmd, shlex_quote(path))
            elif pwd.getpwuid(os.getuid())[0] != self.user:
                user = '-u %s' % shlex_quote(self.user)
        return "%s %s %s" % (self.cron_cmd, user, shlex_quote(path))


# ==================================================

def main():
    # The following example playbooks:
    #
    # - community.general.cronvar: name="SHELL" value="/bin/bash"
    #
    # - name: Set the email
    #   community.general.cronvar: name="EMAILTO" value="doug@ansibmod.con.com"
    #
    # - name: Get rid of the old new host variable
    #   community.general.cronvar: name="NEW_HOST" state=absent
    #
    # Would produce:
    # SHELL = /bin/bash
    # EMAILTO = doug@ansibmod.con.com

    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type='str', required=True),
            value=dict(type='str'),
            user=dict(type='str'),
            cron_file=dict(type='str'),
            insertafter=dict(type='str'),
            insertbefore=dict(type='str'),
            state=dict(type='str', default='present', choices=['absent', 'present']),
            backup=dict(type='bool', default=False),
        ),
        mutually_exclusive=[['insertbefore', 'insertafter']],
        supports_check_mode=False,
    )

    name = module.params['name']
    value = module.params['value']
    user = module.params['user']
    cron_file = module.params['cron_file']
    insertafter = module.params['insertafter']
    insertbefore = module.params['insertbefore']
    state = module.params['state']
    backup = module.params['backup']
    ensure_present = state == 'present'

    changed = False
    res_args = dict()

    # Ensure all files generated are only writable by the owning user.  Primarily relevant for the cron_file option.
    os.umask(int('022', 8))
    cronvar = CronVar(module, user, cron_file)

    module.debug('cronvar instantiated - name: "%s"' % name)

    # --- user input validation ---

    if name is None and ensure_present:
        module.fail_json(msg="You must specify 'name' to insert a new cron variable")

    if value is None and ensure_present:
        module.fail_json(msg="You must specify 'value' to insert a new cron variable")

    if name is None and not ensure_present:
        module.fail_json(msg="You must specify 'name' to remove a cron variable")

    # if requested make a backup before making a change
    if backup:
        dummy, backup_file = tempfile.mkstemp(prefix='cronvar')
        cronvar.write(backup_file)

    if cronvar.cron_file and not name and not ensure_present:
        changed = cronvar.remove_job_file()
        module.exit_json(changed=changed, cron_file=cron_file, state=state)

    old_value = cronvar.find_variable(name)

    if ensure_present:
        if old_value is None:
            cronvar.add_variable(name, value, insertbefore, insertafter)
            changed = True
        elif old_value != value:
            cronvar.update_variable(name, value)
            changed = True
    else:
        if old_value is not None:
            cronvar.remove_variable(name)
            changed = True

    res_args = {
        "vars": cronvar.get_var_names(),
        "changed": changed
    }

    if changed:
        cronvar.write()

    # retain the backup only if crontab or cron file have changed
    if backup:
        if changed:
            res_args['backup_file'] = backup_file
        else:
            os.unlink(backup_file)

    if cron_file:
        res_args['cron_file'] = cron_file

    module.exit_json(**res_args)


if __name__ == '__main__':
    main()
