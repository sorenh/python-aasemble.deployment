#!/usr/bin/env python
#
#   Copyright 2015 Reliance Jio Infocomm, Ltd.
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import argparse
import os
import pipes
import select
import subprocess
import sys
import time

from neutronclient.common.exceptions import Conflict as NeutronConflict

from novaclient.exceptions import Conflict as NovaConflict

from six.moves import configparser

import yaml

from aasemble.deployment import exceptions, utils


def load_yaml(f='.aasemble.yaml', yaml_load=yaml.load):
    with open(f, 'r') as fp:
        return yaml_load(fp)


def load_mappings(f='.aasemble.mappings.ini'):
    with open(f, 'r') as fp:
        return parse_mappings(fp)


def parse_mappings(fp):
    parser = configparser.SafeConfigParser()
    parser.readfp(fp)
    mappings = {}
    for t in ('flavors', 'networks', 'images', 'routers'):
        mappings[t] = {}
        if parser.has_section(t):
            mappings[t].update(parser.items(t))

    return mappings


def find_weak_refs(stack):
    images, flavors, networks = get_images_flavors_and_networks_from_stack(stack)
    dynamic_networks = get_dynamic_networks_from_stack(stack)

    return images, flavors, networks - dynamic_networks


def get_images_flavors_and_networks_from_stack(stack):
    images = set()
    flavors = set()
    networks = set()

    for node_name, node in stack['nodes'].items():
        images.add(node['image'])
        flavors.add(node['flavor'])
        networks.update([n['network'] for n in node['networks']])
    return images, flavors, networks


def get_dynamic_networks_from_stack(stack):
    dynamic_networks = set()
    for network_name, network in stack.get('networks', {}).items():
        dynamic_networks.add(network_name)
    return dynamic_networks


def list_refs(args, stdout=sys.stdout):
    stack = load_yaml(args.stack)
    images, flavors, networks = find_weak_refs(stack)
    if args.tmpl:
        cfg = configparser.SafeConfigParser()
        cfg.add_section('images')
        cfg.add_section('flavors')
        for image in images:
            cfg.set('images', image, '<missing value>')
        for flavor in flavors:
            cfg.set('flavors', flavor, '<missing value>')
        cfg.write(stdout)
    else:
        stdout.write('Images:\n  ')

        if images:
            stdout.write('  '.join(images))
        else:
            stdout.write('None')

        stdout.write('\n\nFlavors:\n  ')

        if flavors:
            stdout.write('  '.join(flavors))
        else:
            stdout.write('None')

        stdout.write('\n')


def run_cmd_once(shell_cmd, real_cmd, environment, deadline):
    proc = subprocess.Popen(shell_cmd,
                            env=environment,
                            shell=True,
                            stdin=subprocess.PIPE)
    stdin = real_cmd + '\n'
    while True:
        if stdin:
            _, rfds, xfds = select.select([], [proc.stdin], [proc.stdin], 1)
            if rfds:
                proc.stdin.write(stdin[0].encode('utf-8'))
                stdin = stdin[1:]
                if not stdin:
                    proc.stdin.close()
            if xfds:
                if proc.stdin.feof():
                    stdin = ''

        if proc.poll() is not None:
            if proc.returncode == 0:
                return True
            else:
                raise exceptions.CommandFailedException(stdin)

        if deadline and time.time() > deadline:
            if proc.poll() is None:
                proc.kill()
            raise exceptions.CommandTimedOutException(stdin)


def get_creds_from_env():
    d = {}
    d['username'] = os.environ['OS_USERNAME']
    d['password'] = os.environ['OS_PASSWORD']
    d['auth_url'] = os.environ['OS_AUTH_URL']
    d['tenant_name'] = os.environ['OS_TENANT_NAME']
    return d


class CloudDriver(object):
    def get_keystone_session(self):
        from keystoneclient import session as keystone_session
        from keystoneclient.auth.identity import v2 as keystone_auth_id_v2
        if 'keystone_session' not in self.conncache:
            self.conncache['keystone_auth'] = keystone_auth_id_v2.Password(**get_creds_from_env())
            self.conncache['keystone_session'] = keystone_session.Session(auth=self.conncache['keystone_auth'])
        return self.conncache['keystone_session']

    def get_keystone_client(self):
        from keystoneclient.v2_0 import client as keystone_client
        if 'keystone' not in self.conncache:
            ks = self.get_keystone_session()
            self.conncache['keystone'] = keystone_client.Client(session=ks)
        return self.conncache['keystone']

    def get_nova_client(self):
        import novaclient.client as novaclient
        if 'nova' not in self.conncache:
            kwargs = {'session': self.get_keystone_session()}
            if 'OS_REGION_NAME' in os.environ:
                kwargs['region_name'] = os.environ['OS_REGION_NAME']
            self.conncache['nova'] = novaclient.Client("2", **kwargs)
        return self.conncache['nova']

    def get_cinder_client(self):
        import cinderclient.client as cinderclient
        if 'cinder' not in self.conncache:
            kwargs = {'session': self.get_keystone_session()}
            if 'OS_REGION_NAME' in os.environ:
                kwargs['region_name'] = os.environ['OS_REGION_NAME']
            self.conncache['cinder'] = cinderclient.Client('1', **kwargs)
        return self.conncache['cinder']

    def get_neutron_client(self):
        import neutronclient.neutron.client as neutronclient
        if 'neutron' not in self.conncache:
            kwargs = {'session': self.get_keystone_session()}
            if 'OS_REGION_NAME' in os.environ:
                kwargs['region_name'] = os.environ['OS_REGION_NAME']
            self.conncache['neutron'] = neutronclient.Client('2.0', **kwargs)
        return self.conncache['neutron']

    def get_networks(self):
        return self.get_neutron_client().list_networks()['networks']

    def get_ports(self):
        return self.get_neutron_client().list_ports()['ports']

    def get_floating_ips(self):
        return self.get_neutron_client().list_floatingips()['floatingips']

    def get_security_groups(self):
        return self.get_neutron_client().list_security_groups()['security_groups']

    def delete_port(self, uuid):
        self.get_neutron_client().delete_port(uuid)

    def delete_network(self, uuid):
        self.get_neutron_client().delete_network(uuid)

    def delete_router(self, uuid):
        self.get_neutron_client().delete_router(uuid)

    def delete_subnet(self, uuid):
        nc = self.get_neutron_client()
        try:
            nc.delete_subnet(uuid)
        except NeutronConflict:
            # This is probably due to the router port. Let's find it.
            router_found = False
            for port in nc.list_ports(device_owner='network:router_interface')['ports']:
                for fixed_ip in port['fixed_ips']:
                    if fixed_ip['subnet_id'] == uuid:
                        router_found = True
                        nc.remove_interface_router(port['device_id'],
                                                   {'subnet_id': uuid})
                        break
            if router_found:
                # Let's try again
                nc.delete_subnet(uuid)
            else:
                # Ok, we didn't find a router, so clearly this is a different
                # problem. Just re-raise the original exception.
                raise

    def delete_secgroup(self, uuid):
        self.get_neutron_client().delete_security_group(uuid)

    def delete_secgroup_rule(self, uuid):
        nc = self.get_neutron_client()
        nc.delete_security_group_rule(uuid)

    def delete_floatingip(self, uuid):
        nc = self.get_neutron_client()
        nc.delete_floatingip(uuid)

    def create_port(self, name, network, network_id, secgroups):
        nc = self.get_neutron_client()
        port = {'name': name,
                'admin_state_up': True,
                'network_id': network_id,
                'security_groups': secgroups}
        port = nc.create_port({'port': port})['port']

        return {'id': port['id'],
                'fixed_ip': port['fixed_ips'][0]['ip_address'],
                'mac': port['mac_address'],
                'network_name': network}

    def find_floating_network(self, ):
        nc = self.get_neutron_client()
        networks = nc.list_networks(**{'router:external': True})
        return networks['networks'][0]['id']

    def create_floating_ip(self, record_resource):
        nc = self.get_neutron_client()
        floating_network = self.find_floating_network()
        floatingip = {'floating_network_id': floating_network}
        floatingip = nc.create_floatingip({'floatingip': floatingip})
        record_resource('floatingip', floatingip['floatingip']['id'])
        return (floatingip['floatingip']['id'],
                floatingip['floatingip']['floating_ip_address'])

    def associate_floating_ip(self, port_id, fip_id):
        nc = self.get_neutron_client()
        nc.update_floatingip(fip_id, {'floatingip': {'port_id': port_id}})

    def create_network(self, name, info, mappings, record_resource):
        nc = self.get_neutron_client()
        network = {'name': name, 'admin_state_up': True}
        network = nc.create_network({'network': network})
        record_resource('network', network['network']['id'])

        subnet = {"network_id": network['network']['id'],
                  "ip_version": 4,
                  "cidr": info['cidr'],
                  "name": name}
        subnet = nc.create_subnet({'subnet': subnet})['subnet']
        record_resource('subnet', subnet['id'])

        if '*' in mappings.get('routers', {}):
            nc.add_interface_router(mappings['routers']['*'], {'subnet_id': subnet['id']})

        return network['network']['id']

    def create_security_group(self, base_name, name, info, record_resource, secgroups):
        nc = self.get_neutron_client()

        secgroup = {'name': name}
        secgroup = nc.create_security_group({'security_group': secgroup})['security_group']

        record_resource('secgroup', secgroup['id'])
        secgroups[base_name] = secgroup['id']

        for rule in (info or []):
            secgroup_rule = {"direction": "ingress",
                             "ethertype": "IPv4",
                             "port_range_min": rule['from_port'],
                             "port_range_max": rule['to_port'],
                             "protocol": rule['protocol'],
                             "security_group_id": secgroup['id']}

            if 'source_group' in rule:
                secgroup_rule['remote_group_id'] = secgroups.get(rule['source_group'], rule['source_group'])
            else:
                secgroup_rule['remote_ip_prefix'] = rule['cidr']

            secgroup_rule = nc.create_security_group_rule({'security_group_rule': secgroup_rule})
            record_resource('secgroup_rule', secgroup_rule['security_group_rule']['id'])

    def get_servers(self):
        return self.get_nova_client().servers.list()

    def delete_keypair(self, name):
        nc = self.get_nova_client()
        nc.keypairs.delete(name)

    def delete_server(self, uuid):
        nc = self.get_nova_client()
        nc.servers.delete(uuid)

    def create_keypair(self, name, keydata, retry_count):
        nc = self.get_nova_client()
        attempts_left = retry_count + 1
        while attempts_left > 0:
            try:
                nc.keypairs.create(name, keydata)
                break
            except NovaConflict:
                return
            except Exception as e:
                if attempts_left == 0:
                    raise
                print(e)
                attempts_left -= 1

    def create_volume(self, size, image_ref, retry_count, record_resource):
        cc = self.get_cinder_client()
        attempts_left = retry_count + 1
        while attempts_left > 0:
            try:
                volume = cc.volumes.create(size=size,
                                           imageRef=image_ref)
                record_resource('volume', volume.id)
                return volume
            except Exception as e:
                if attempts_left == 0:
                    raise
                print(e)
                attempts_left -= 1


class Node(object):
    def __init__(self, name, info, runner, keypair=None, userdata=None):
        self.record_resource = lambda *args, **kwargs: None
        self.name = name
        self.info = info
        self.runner = runner
        self.keypair = keypair
        self.userdata = userdata
        self.server_id = None
        self.fip_ids = set()
        self.ports = []
        self.server_status = None
        self.image = None
        self.flavor = None
        self.attempts_left = runner.retry_count + 1

        if self.info.get('image') in self.runner.mappings.get('images', {}):
            self.info['image'] = self.runner.mappings['images'][self.info['image']]

        if self.info.get('flavor') in self.runner.mappings.get('flavors', {}):
            self.info['flavor'] = self.runner.mappings['flavors'][self.info['flavor']]

    def poll(self, desired_status='ACTIVE'):
        """
        This one poll nova and return the server status
        """
        if self.server_status != desired_status:
            self.server_status = self.runner.cloud_driver.get_nova_client().servers.get(self.server_id).status
        return self.server_status

    def clean(self):
        """
        Cleaner: This method remove server, fip, port etc.
        We could keep fip and may be ports (ports are getting deleted with current
        neutron client), but that is going to be bit more complex to make sure
        right port is assigned to right fip etc, so atm, just removing them.
        """
        for fip_id in self.fip_ids:
            self.runner.delete_floatingip(fip_id)
        self.fip_ids = set()

        for port in self.ports:
            self.runner.delete_port(port['id'])
        self.ports = []

        self.runner.delete_server(self.server_id)
        self.server_id = None

    def create_nics(self, networks):
        nics = []
        for eth_idx, network in enumerate(networks):
            port_name = '%s_eth%d' % (self.name, eth_idx)
            port_info = self.runner.create_port(port_name, network['network'],
                                                [self.runner.secgroups[secgroup] for secgroup in network.get('securitygroups', [])])
            self.runner.record_resource('port', port_info['id'])
            self.ports.append(port_info)

            if network.get('assign_floating_ip', False):
                fip_id, fip_address = self.runner.create_floating_ip()
                self.runner.associate_floating_ip(port_info['id'], fip_id)
                port_info['floating_ip'] = fip_address
                self.fip_ids.add(fip_id)

            nics.append(port_info['id'])
        return nics

    def build(self):
        if self.flavor is None:
            self.flavor = self.runner.cloud_driver.get_nova_client().flavors.get(self.info['flavor'])

        nics = [{'port-id': port_id} for port_id in self.create_nics(self.info['networks'])]

        volume = self.runner.create_volume(size=self.info['disk'],
                                           image_ref=self.info['image'])

        while volume.status != 'available':
            time.sleep(3)
            volume = self.runner.cloud_driver.get_cinder_client().volumes.get(volume.id)

        bdm = {'vda': '%s:::1' % (volume.id,)}

        server = self.runner.cloud_driver.get_nova_client().servers.create(self.name, image=None,
                                                                           block_device_mapping=bdm,
                                                                           flavor=self.flavor, nics=nics,
                                                                           key_name=self.keypair, userdata=self.userdata)
        self.runner.record_resource('server', server.id)
        self.server_id = server.id
        self.attempts_left -= 1

    @property
    def floating_ip(self):
        for port in self.ports:
            if 'floating_ip' in port:
                return port['floating_ip']


class DeploymentRunner(object):
    def __init__(self, config=None, suffix=None, mappings=None, key=None,
                 record_resource=None, retry_count=0, cloud_driver=None):
        self.cfg = config
        self.suffix = suffix
        self.mappings = mappings or {}
        self.key = key
        self.retry_count = retry_count
        self.cloud_driver = cloud_driver
        self.record_resource = lambda *args, **kwargs: None

        self.conncache = {}
        self.networks = {}
        self.secgroups = {}
        self.nodes = {}

    def _map_network(self, network):
        if network in self.mappings.get('networks', {}):
            return self.mappings['networks'][network]
        elif network in self.networks:
            return self.networks[network]
        return network

    def detect_existing_resources(self):
        suffix = self.add_suffix('')
        if suffix:
            def strip_suffix(s):
                return s[:-len(suffix)]
        else:
            def strip_suffix(s):
                return s

        network_name_by_id = {}

        for network in self.cloud_driver.get_networks():
            if network['name'].endswith(suffix):
                base_name = strip_suffix(network['name'])
                if base_name in self.networks:
                    raise exceptions.DuplicateResourceException('Network', network['name'])

                self.networks[base_name] = network['id']
                network_name_by_id[network['id']] = base_name

        raw_ports = [{'id': port['id'],
                      'fixed_ip': port['fixed_ips'][0]['ip_address'],
                      'mac': port['mac_address'],
                      'network_name': network_name_by_id.get(port['network_id'], port['network_id'])}
                     for port in self.cloud_driver.get_ports()]
        ports_by_id = {port['id']: port for port in raw_ports}
        ports_by_mac = {port['mac']: port for port in raw_ports}

        for fip in self.cloud_driver.get_floating_ips():
            port_id = fip['port_id']
            if not port_id:
                continue
            port = ports_by_id[port_id]
            port['floating_ip'] = fip['floating_ip_address']

        for secgroup in self.cloud_driver.get_security_groups():
            if secgroup['name'].endswith(suffix):
                base_name = strip_suffix(secgroup['name'])
                if base_name in self.secgroups:
                    raise exceptions.DuplicateResourceException('Security Group', secgroup['name'])

                self.secgroups[base_name] = secgroup['id']

        for node in self.cloud_driver.get_servers():
            if node.name.endswith(suffix):
                base_name = strip_suffix(node.name)
                if base_name in self.nodes:
                    raise exceptions.DuplicateResourceException('Node', node.name)

                self.nodes[base_name] = Node(node.name, {}, self)
                for address in node.addresses.values():
                    mac = address[0]['OS-EXT-IPS-MAC:mac_addr']
                    port = ports_by_mac[mac]
                    self.nodes[base_name].ports.append(port)

    def delete_volume(self, uuid):
        cc = self.get_cinder_client()
        cc.volumes.delete(uuid)

    def delete_port(self, uuid):
        self.cloud_driver.delete_port(uuid)

    def delete_network(self, uuid):
        self.cloud_driver.delete_network(uuid)

    def delete_router(self, uuid):
        self.cloud_driver.delete_router(uuid)

    def delete_subnet(self, uuid):
        self.cloud_driver.delete_subnet(uuid)

    def delete_secgroup(self, uuid):
        self.cloud_driver.delete_secgroup(uuid)

    def delete_secgroup_rule(self, uuid):
        self.cloud_driver.delete_secgroup_rule(uuid)

    def delete_floatingip(self, uuid):
        self.cloud_driver.delete_floatingip(uuid)

    def delete_keypair(self, name):
        self.cloud_driver.delete_keypair(name)

    def delete_server(self, uuid):
        self.cloud_driver.delete_server(uuid)

    def create_volume(self, size, image_ref):
        return self.cloud_driver.create_volume(size, image_ref, self.retry_count, self.record_resource)

    def create_port(self, name, network, secgroups):
        network_id = self._map_network(network)
        return self.cloud_driver.create_port(name, network, network_id, secgroups)

    def create_keypair(self, name, keydata):
        self.cloud_driver.create_keypair(name, keydata, self.retry_count)

    def create_floating_ip(self):
        return self.cloud_driver.create_floating_ip(record_resource=self.record_resource)

    def associate_floating_ip(self, port_id, fip_id):
        self.cloud_driver.associate_floating_ip(port_id, fip_id)

    def create_network(self, name, info):
        return self.cloud_driver.create_network(name, info, self.mappings, self.record_resource)

    def create_security_group(self, base_name, info):
        name = self.add_suffix(base_name)
        self.cloud_driver.create_security_group(base_name, name, info, self.record_resource, self.secgroups)

    def build_env_prefix(self, details):
        env_prefix = ''

        def add_environment(key, value):
            return '%s=%s ' % (pipes.quote(key), pipes.quote(value or ''))

        env_prefix += add_environment('ALL_NODES',
                                      ' '.join([self.add_suffix(s) for s in self.nodes.keys()]))

        for node_name in self.nodes:
            node = self.nodes[node_name]
            if node.info.get('export', False):
                for port in node.ports:
                    key = 'AASEMBLE_%s_%s_fixed' % (node_name, port['network_name'])
                    value = port['fixed_ip']
                    env_prefix += add_environment(key, value)

        if 'environment' in details:
            for key, value in details['environment'].items():
                if value.startswith('$'):
                    value = os.environ.get(value[1:])
                env_prefix += add_environment(key, value)

        return env_prefix

    def shell_step(self, details, environment=None):
        env_prefix = self.build_env_prefix(details)

        cmd = self.shell_step_cmd(details, env_prefix)

        if details.get('total-timeout', False):
            overall_deadline = time.time() + utils.parse_time(details['total-timeout'])
        else:
            overall_deadline = None

        if details.get('timeout', False):
            individual_exec_limit = utils.parse_time(details['timeout'])
        else:
            individual_exec_limit = None

        if details.get('retry-delay', False):
            retry_delay = utils.parse_time(details['retry-delay'])
        else:
            retry_delay = 0

        def wait():
            time.sleep(retry_delay)

        # Four settings matter here:
        # retry-if-fails: True/False
        # retry-delay: Time to wait between retries
        # timeout: Max time per command execution
        # total-timeout: How long time to spend on this in total
        while True:
            if individual_exec_limit:
                deadline = time.time() + individual_exec_limit
                if overall_deadline:
                    if deadline > overall_deadline:
                        deadline = overall_deadline
            elif overall_deadline:
                deadline = overall_deadline
            else:
                deadline = None

            try:
                run_cmd_once(cmd, details['cmd'], environment, deadline)
                break
            except exceptions.CommandFailedException:
                if details.get('retry-if-fails', False):
                    wait()
                    continue
                raise
            except exceptions.CommandTimedOutException:
                if details.get('retry-if-fails', False):
                    if time.time() + retry_delay < deadline:
                        wait()
                        continue
                raise

    def shell_step_cmd(self, details, env_prefix=''):
        if details.get('type', None) == 'remote':
            fip_addr = self.nodes[details['node']].floating_ip
            return 'ssh -o StrictHostKeyChecking=no ubuntu@%s "%s bash"' % (fip_addr, env_prefix)
        else:
            return '%s bash' % (env_prefix,)

    def add_suffix(self, s):
        if self.suffix:
            return '%s_%s' % (s, self.suffix)
        else:
            return s

    def provision_step(self, details):
        stack = load_yaml(details['stack'])

        if self.key:
            keypair_name = self.add_suffix('pubkey')
            self.create_keypair(keypair_name, self.key)
            self.record_resource('keypair', keypair_name)
        else:
            keypair_name = None

        if 'userdata' in details:
            with open(details['userdata'], 'r') as fp:
                userdata = fp.read()
        else:
            userdata = None

        pending_nodes = set()

        def wait():
            time.sleep(5)

        for base_network_name, network_info in stack['networks'].items():
            if base_network_name in self.networks:
                continue
            network_name = self.add_suffix(base_network_name)
            self.networks[base_network_name] = self.create_network(network_name,
                                                                   network_info)

        for base_secgroup_name, secgroup_info in stack['securitygroups'].items():
            if base_secgroup_name in self.secgroups:
                continue
            self.create_security_group(base_secgroup_name, secgroup_info)

        for base_node_name, node_info in stack['nodes'].items():
            if 'number' in node_info:
                count = node_info.pop('number')
                for idx in range(1, count + 1):
                    node_name = '%s%d' % (base_node_name, idx)
                    name = self._create_node(node_name, node_info,
                                             keypair_name=keypair_name, userdata=userdata)
                    if name:
                        pending_nodes.add(name)
            else:
                name = self._create_node(base_node_name, node_info,
                                         keypair_name=keypair_name, userdata=userdata)
                if name:
                    pending_nodes.add(name)

        while True:
            pending_nodes = self._poll_pending_nodes(pending_nodes)
            if not pending_nodes:
                break
            wait()

    def _create_node(self, base_name, node_info, keypair_name, userdata):
        if base_name in self.nodes:
            return
        node_name = self.add_suffix(base_name)
        self.nodes[base_name] = Node(node_name, node_info,
                                     runner=self,
                                     keypair=keypair_name,
                                     userdata=userdata)
        self.nodes[base_name].build()
        return base_name

    def _poll_pending_nodes(self, pending_nodes):
        done = set()
        for name in pending_nodes:
            state = self.nodes[name].poll()
            if state == 'ACTIVE':
                done.add(name)
            elif state == 'ERROR':
                if self.retry_count:
                    self.nodes[name].clean()
                    if self.nodes[name].attempts_left:
                        self.nodes[name].build()
                        continue
                raise exceptions.ProvisionFailedException()
        return pending_nodes.difference(done)

    def deploy(self, name):
        for step in self.cfg[name]:
            step_type = step.keys()[0]
            details = step[step_type]
            func = getattr(self, '%s_step' % step_type)
            func(details)


def main(argv=sys.argv[1:], stdout=sys.stdout):
    def deploy(args):
        cfg = load_yaml(args.cfg)

        if args.key:
            with open(args.key, 'r') as fp:
                key = fp.read()

        dr = DeploymentRunner(config=cfg,
                              suffix=args.suffix,
                              mappings=load_mappings(args.mappings),
                              key=key,
                              retry_count=args.retry_count,
                              cloud_driver=CloudDriver())

        if args.cont:
            dr.detect_existing_resources()

        if args.cleanup:
            with open(args.cleanup, 'a+') as cleanup:
                def record_resource(type_, id):
                    cleanup.write('%s: %s\n' % (type_, id))
                dr.record_resource = record_resource

                dr.deploy(args.name)
        else:
            dr.deploy(args.name)

    def cleanup(args):
        dr = DeploymentRunner(cloud_driver=CloudDriver())

        with open(args.log, 'r') as fp:
            lines = [l.strip() for l in fp]

        lines.reverse()
        for l in lines:
            resource_type, uuid = l.split(': ')
            func = getattr(dr, 'delete_%s' % resource_type)
            try:
                func(uuid)
            except Exception as e:
                print(e)

    parser = argparse.ArgumentParser(description='Run deployment')

    subparsers = parser.add_subparsers(help='Subcommand help')
    list_refs_parser = subparsers.add_parser('list-refs',
                                             help='List symbolic resources')
    list_refs_parser.set_defaults(func=list_refs)
    list_refs_parser.add_argument('--tmpl', action='store_true',
                                  help='Output template ini file')
    list_refs_parser.add_argument('stack', help='YAML file describing stack')

    deploy_parser = subparsers.add_parser('deploy', help='Perform deployment')
    deploy_parser.set_defaults(func=deploy)
    deploy_parser.add_argument('--cfg', default='.aasemble.yaml',
                               help='Deployment config file')
    deploy_parser.add_argument('--suffix', help='Resource name suffix')
    deploy_parser.add_argument('--mappings', help='Resource map file')
    deploy_parser.add_argument('--key', help='Public key file')
    deploy_parser.add_argument('--cleanup', help='Cleanup file')
    deploy_parser.add_argument('--retry-count', type=int, default=0,
                               help='Retry RETRY-COUNT times before giving up provisioning a VM')
    deploy_parser.add_argument('--incremental', dest='cont', action='store_true',
                               help="Don't create resources if identically named ones already exist")
    deploy_parser.add_argument('name', help='Deployment to perform')

    cleanup_parser = subparsers.add_parser('cleanup', help='Clean up')
    cleanup_parser.set_defaults(func=cleanup)
    cleanup_parser.add_argument('log', help='Clean up log (generated by deploy)')

    args = parser.parse_args(argv)

    if args.func:
        args.func(args)

if __name__ == '__main__':
    main()
