#!/usr/bin/env python
# -*- coding: utf-8 -*-

'''
External inventory script for Abiquo
====================================

Shamelessly copied from an existing inventory script.

This script generates an inventory that Ansible can understand by making API requests to Abiquo API
Requires some python libraries, ensure to have them installed when using this script.

This script has been tested in Abiquo 3.0 but it may work also for Abiquo 2.6.

Before using this script you may want to modify abiquo.ini config file.

This script generates an Ansible hosts file with these host groups:

ABQ_xxx: Defines a hosts itself by Abiquo VM name label
all: Contains all hosts defined in Abiquo user's enterprise
virtualdatecenter: Creates a host group for each virtualdatacenter containing all hosts defined on it
virtualappliance: Creates a host group for each virtualappliance containing all hosts defined on it
imagetemplate: Creates a host group for each image template containing all hosts using it

'''

# (c) 2014, Daniel Beneyto <daniel.beneyto@abiquo.com>
#
# This file is part of Ansible,
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

import os
import copy
import sys
import traceback
import time
import ConfigParser
import requests
import argparse
import httplib as http_client
import pdb
from abiquo.client import Abiquo, check_response
from requests_oauthlib import OAuth1

try:
    import json
except ImportError:
    import simplejson as json

if 'ABQ_DEBUG' in os.environ:
    http_client.HTTPConnection.debuglevel = 1

class AbiquoInventory(object):
    def _empty_inventory(self):
        return {"_meta": {"hostvars": {}}}

    def __init__(self):
        ''' Main execution path '''
        self.inventory = self._empty_inventory()

        '''Initialise'''
        self.parse_cli_args()
        self.get_config()
        self.init_client()

        if self.args.refresh_cache:
            inv = self.generate_inv_from_api()
        elif not self.cache_available():
            inv = self.generate_inv_from_api()
        else:
            inv = self.get_cache()

        self.save_cache(inv)

        # if self.args.host:
        sys.stdout.write(json.dumps(inv, sort_keys=True, indent=2))

    def get_config(self):
        ''' Read config file '''
        config = ConfigParser.SafeConfigParser()
        for configfilename in [os.path.splitext(os.path.abspath(sys.argv[0]))[0] + '.ini', './abiquo_inventory.ini']:
            if os.path.exists(configfilename):
                config.read(configfilename)
                self.config = config
                break

    def init_client(self):
        creds = None
        api_url = self.config.get('api', 'uri')
        ssl_verify = self.config.getboolean('api', 'ssl_verify')
        if not ssl_verify:
            import urllib3
            urllib3.disable_warnings()

        if self.config.has_option('auth', 'api_key'):
            # Use OAuth1 app
            api_key = self.config.get('auth', 'api_key')
            api_secret = self.config.get('auth', 'api_secret')
            token = self.config.get('auth', 'token')
            token_secret = self.config.get('auth', 'token_secret')
            creds = OAuth1(api_key, client_secret=api_secret, resource_owner_key=token, resource_owner_secret=token_secret)
        else:
            creds = (self.config.get('auth', 'apiuser'), self.config.get('auth', 'apipass'))

        self.api = Abiquo(api_url, auth=creds, verify=ssl_verify)

    def cache_available(self):
        ''' checks if we have a 'fresh' cache available for item requested '''
        if self.config.has_option('cache','cache_dir'):
            dpath = self.config.get('cache','cache_dir')

            try:
                existing = os.stat( '/'.join([dpath,'abiquo-inventory']))
            except:
                # cache doesn't exist or isn't accessible
                return False

            if self.config.has_option('cache', 'cache_max_age'):
                maxage = self.config.get('cache', 'cache_max_age')
                if ((int(time.time()) - int(existing.st_mtime)) <= int(maxage)):
                    return True

        return False

    def get_cache(self):
        ''' returns cached item  '''
        dpath = self.config.get('cache','cache_dir')
        inv = {}
        try:
            cache = open('/'.join([dpath,'abiquo-inventory']), 'r')
            inv = json.loads(cache.read())
            cache.close()
        except IOError as e:
            pass # not really sure what to do here

        return inv

    def save_cache(self, data):
        ''' saves item to cache '''
        dpath = self.config.get('cache','cache_dir')
        try:
            cache = open('/'.join([dpath,'abiquo-inventory']), 'w')
            cache.write(json.dumps(data))
            cache.close()
        except IOError as e:
            pass # not really sure what to do here

    def get_vms(self):
        code, vms = self.api.cloud.virtualmachines.get(headers={'accept':'application/vnd.abiquo.virtualmachines+json'})
        try:
            check_response(200, code, vms)
        except Exception as e:
            self.fail_with_error(e)
        return vms

    def update_vm_metadata(self, vm):
        code, metadata = vm.follow('metadata').get()
        try:
            check_response(200, code, metadata)
        except Exception as e:
            self.fail_with_error(e)
        vm.json['metadata'] = metadata.json

    def update_vm_template(self, vm):
        template = self.get_vm_template(vm)
        json = template.json
        del json['links']
        vm.json['template'] = json

    def update_vm_disks_and_nics(self, vm):
        vm_nics = []
        vm_disks = []
        vm_vols = []
        nics = self.get_vm_nics(vm)
        disks = self.get_vm_disks(vm)
        vols = self.get_vm_volumes(vm)

        for nic in nics:
            vm_nics.append(nic.json)

        for disk in disks:
            vm_disks.append(disk.json)

        for vol in vols:
            vm_disks.append(vol.json)

        vm.json['nics'] = vm_nics
        vm.json['disks'] = vm_disks

    def get_vm_template(self, vm):
        code, template = vm.follow('virtualmachinetemplate').get()
        try:
            check_response(200, code, template)
        except Exception as e:
            fail_with_error(e)
        return template

    def get_vm_nics(self, vm):
        code, nics = vm.follow('nics').get()
        try:
            check_response(200, code, nics)
        except Exception as e:
            fail_with_error(e)
        return nics

    def get_vm_disks(self, vm):
        code, disks = vm.follow('harddisks').get()
        try:
            check_response(200, code, disks)
        except Exception as e:
            fail_with_error(e)

        return disks

    def get_vm_volumes(self, vm):
        code, vols = vm.follow('volumes').get()
        try:
            check_response(200, code, vols)
        except Exception as e:
            fail_with_error(e)

        return vols

    def nic_json_to_dict(self, nics_json):
        nics = copy.copy(nics_json)
        nic_dict = {}

        for nic in nics:
            nic_rel = "nic%d" % nic['sequence']
            for key in nic:
                if key != 'links':
                    nic_dict["%s_%s" % (nic_rel, key)] = nic[key]

            netlink = filter(lambda x: "network" in x['rel'], nic['links'])
            if len(netlink) > 0:
                netlink = netlink[0]
                nic_dict["%s_net_type" % nic_rel] = netlink['rel']

        return nic_dict

    def disk_json_to_dict(self, disks_json):
        disks = copy.copy(disks_json)
        disk_dict = {}

        for disk in disks:
            disk_rel = "disk%d" % disk['sequence']
            for key in disk:
                if key != 'links':
                    disk_dict["%s_%s" % (disk_rel, key)] = disk[key]

            tierlink = filter(lambda x: "tier" in x['rel'], disk['links'])
            if len(tierlink) > 0:
                tierlink = tierlink[0]
                disk_dict["%s_tier" % disk_rel] = tierlink['title']

        return disk_dict

    def vars_from_json(self, vm_json):
        vm = copy.copy(vm_json)
        nics_dict = self.nic_json_to_dict(vm['nics'])
        disks_dict = self.disk_json_to_dict(vm['disks'])

        host_vars = dict(nics_dict.items() + disks_dict.items())

        link_rels = [
            "category", "virtualmachinetemplate", "hypervisortype", "ip", "location", "hardwareprofile",
            "state", "network_configuration", "virtualappliance", "virtualdatacenter", "user", "enterprise"
        ]
        vm_links = copy.copy(vm['links'])
        link_dict = {}
        for rel in link_rels:
            links = filter(lambda y: y['rel'] == rel, vm_links)
            if len(links) > 0:
                link = links[0]
                k = link['rel']
                v = link['title']
                link_dict[k] = v

        attrs_dict = copy.copy(vm)
        del attrs_dict['links']
        del attrs_dict['nics']
        del attrs_dict['disks']

        d = dict(host_vars.items() + link_dict.items() + attrs_dict.items())
        for i in d:
            if not i.startswith('abq'):
                d['abq_%s' % i] = d.pop(i)

        return d

    def generate_inv_from_api(self):
        inventory = self.inventory
        try:
            vms = self.get_vms()
            config = self.config
            for vm in vms:
                self.update_vm_disks_and_nics(vm)
                self.update_vm_template(vm)
                if config.getboolean('defaults', 'get_metadata') is True:
                    self.update_vm_metadata(vm)

                host_vars = self.vars_from_json(vm.json)

                hw_profile = ''
                for link in vm.links:
                    if link['rel'] == 'virtualappliance':
                        vm_vapp = link['title'].replace('[','').replace(']','').replace(' ','_')
                    elif link['rel'] == 'virtualdatacenter':
                        vm_vdc = link['title'].replace('[','').replace(']','').replace(' ','_')
                    elif link['rel'] == 'virtualmachinetemplate':
                        vm_template = link['title'].replace('[','').replace(']','').replace(' ','_')
                    elif link['rel'] == 'hardwareprofile':
                        hw_profile = link['title'].replace('[','').replace(']','').replace(' ','_')

                # From abiquo.ini: Only adding to inventory VMs with public IP
                if config.getboolean('defaults', 'public_ip_only') is True:
                    for link in vm.links:
                        if (link['type']=='application/vnd.abiquo.publicip+json' and link['rel']=='ip'):
                            vm_nic = link['title']
                            break
                        else:
                            vm_nic = None
                # Otherwise, assigning defined network interface IP address
                else:
                    for link in vm.links:
                        if link['rel'] == config.get('defaults', 'default_net_interface'):
                            vm_nic = link['title']
                            break
                        else:
                            vm_nic = None

                if vm_nic is None:
                    continue

                vm_state = True
                # From abiquo.ini: Only adding to inventory VMs deployed
                if config.getboolean('defaults', 'deployed_only') is True and vm.state == 'NOT_ALLOCATED':
                    vm_state = False

                if not vm_state:
                    continue

                ## Set host vars
                inventory['_meta']['hostvars'][vm_nic] = host_vars

                ## Start with groupings

                # VM template
                vm_tmpl = "template_%s" % vm_template
                if vm_tmpl not in inventory:
                    inventory[vm_tmpl] = []
                inventory[vm_tmpl].append(vm_nic)

                # vApp
                vapp = "vapp_%s" % vm_vapp
                if vapp not in inventory:
                    inventory[vapp] = []
                inventory[vapp].append(vm_nic)

                # VDC
                vdc = "vdc_%s" % vm_vdc
                if vdc not in inventory:
                    inventory[vdc] = []
                inventory[vdc].append(vm_nic)

                # VDC_vApp
                vdcvapp = 'vdc_%s_vapp_%s' % (vm_vdc, vm_vapp)
                if vdcvapp not in inventory:
                    inventory[vdcvapp] = []
                inventory[vdcvapp].append(vm_nic)

                # HW profiles
                if hw_profile != '':
                    hwprof = 'hwprof_%s' % hw_profile
                    if hwprof not in inventory:
                        inventory[hwprof] = []
                    inventory[hwprof].append(vm_nic)

                # VM variables
                if 'variables' in vm.json:
                    for var in vm.json['variables']:
                        var_sane = var.replace('[','').replace(']','').replace(' ','_')
                        val_sane = vm.json['variables'][var].replace('[','').replace(']','').replace(' ','_')
                        vargroup = "var_%s_%s" % (var_sane, val_sane)
                        if vargroup not in inventory:
                            inventory[vargroup] = []
                        inventory[vargroup].append(vm_nic)

            return inventory
        except Exception as e:
            # Return empty hosts output
            sys.stderr.write(traceback.format_exc())
            return self._empty_inventory()
    
    def fail_with_error(self, e):
        sys.stderr.write(str(e))
        sys.stderr.write(traceback.format_exc())
        sys.exit(1)

    def parse_cli_args(self):
        ''' Command line argument processing '''
        parser = argparse.ArgumentParser(description='Produce an Ansible Inventory file based on Abiquo VMs')
        parser.add_argument('--list', action='store_true', default=True,
                            help='List VMs (default: True)')
        parser.add_argument('--host', action='store',
                            help='Get all the variables about a specific VM')
        parser.add_argument('--refresh-cache', action='store_true', default=False,
                            help='Force refresh of cache by making API requests (default: False - use cache files)')
        self.args = parser.parse_args()

if __name__ == '__main__':
    AbiquoInventory()
