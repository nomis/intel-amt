# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# This file is heavily derived from the Ironic AMT driver at
# https://github.com/openstack/ironic/tree/master/ironic/drivers/modules/amt
#
# Thanks much for the hard work of people in that project to produce
# Open Source software that acts as one of the few bits of example
# code for this interface.
#
# KVM Redirection derived from https://github.com/Ylianst/MeshCommander
# (amt-redir-ws-0.1.0.js by Ylian Saint-Hilaire)

import xml.dom.minidom
from xml.etree import ElementTree

import requests
from requests.auth import HTTPDigestAuth

import hashlib
import pem
import os
import secrets
import select
import socket
import ssl
import stat
import time
import uuid

import amt.wsman


"""CIM schema urls

Conceptually you can query a Service, everything else is for update
only or modeling only. And, yes this is as redundant as it looks.
"""

SCHEMA_BASE = 'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/'

CIM_AssociatedPowerManagementService = (SCHEMA_BASE +
                                        'CIM_AssociatedPowerManagementService')
CIM_PowerManagementService = (SCHEMA_BASE +
                              'CIM_PowerManagementService')
CIM_BootService = SCHEMA_BASE + 'CIM_BootService'

CIM_ComputerSystem = SCHEMA_BASE + 'CIM_ComputerSystem'
CIM_ComputerSystemPackage = SCHEMA_BASE + 'CIM_ComputerSystemPackage'
CIM_BootConfigSetting = SCHEMA_BASE + 'CIM_BootConfigSetting'
CIM_BootSourceSetting = SCHEMA_BASE + 'CIM_BootSourceSetting'
CIM_KVMRedirectionSAP = SCHEMA_BASE + 'CIM_KVMRedirectionSAP'

SCHEMA_BASE = 'http://intel.com/wbem/wscim/1/amt-schema/1/'

AMT_PublicKeyManagementService = SCHEMA_BASE + 'AMT_PublicKeyManagementService'
AMT_PublicKeyCertificate = SCHEMA_BASE + 'AMT_PublicKeyCertificate'
AMT_PublicPrivateKeyPair = SCHEMA_BASE + 'AMT_PublicPrivateKeyPair'
AMT_TLSSettingData = SCHEMA_BASE + 'AMT_TLSSettingData'
AMT_TLSCredentialContext = SCHEMA_BASE + 'AMT_TLSCredentialContext'
AMT_SetupAndConfigurationService = SCHEMA_BASE + 'AMT_SetupAndConfigurationService'
AMT_TimeSynchronizationService = SCHEMA_BASE + 'AMT_TimeSynchronizationService'

del SCHEMA_BASE

# Additional useful constants
_SOAP_ENVELOPE = 'http://www.w3.org/2003/05/soap-envelope'
_SOAP_ENUMERATION = 'http://schemas.xmlsoap.org/ws/2004/09/enumeration'
_ADDRESS = 'http://schemas.xmlsoap.org/ws/2004/08/addressing'
_ANONYMOUS = 'http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous'
_WSMAN = 'http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd'

_TLS_REMOTE = 'Intel(r) AMT 802.3 TLS Settings'
_TLS_LOCAL = 'Intel(r) AMT LMS TLS Settings'
_TLS_EP_COLLECTION = 'TLSProtocolEndpointInstances Collection'


# magic ports to connect to
AMT_PROTOCOL_PORT_MAP = {
    'http': 16992,
    'https': 16993,
}


def pp_xml(body):
    """Pretty print format some XML so it's readable."""
    pretty = xml.dom.minidom.parseString(body)
    return pretty.toprettyxml(indent="  ")


class Client(object):
    """AMT client.

    Manage interactions with AMT host.
    """
    def __init__(self, address, password,
                 username=None, protocol='http',
                 vncpasswd=None, ca=None, key=None, cert=None):
        port = AMT_PROTOCOL_PORT_MAP[protocol]
        self.path = '/wsman'
        self.uri = "%(protocol)s://%(address)s:%(port)s%(path)s" % {
            'address': address,
            'protocol': protocol,
            'port': port,
            'path': self.path}
        self.address = address
        self.protocol = protocol
        self.port = port
        self.username = username if username is not None else 'admin'
        self.password = password
        self.vncpassword = vncpasswd
        self.session = requests.Session()
        self.session.auth = HTTPDigestAuth(self.username, self.password)
        self.session.verify = ca if ca is not None else False
        if key is not None and cert is not None:
            self.session.cert = (cert, key)

    def post(self, payload, ns=None):
        resp = self.session.post(self.uri,
                                 headers={'content-type':
                                          'application/soap+xml;charset=UTF-8'},
                                 data=payload)
        resp.raise_for_status()
        self.version = resp.headers.get('Server')
        if ns:
            rv = _return_value(resp.content, ns)
            if rv == 0:
                return 0
            print(pp_xml(resp.content))
            return rv
        else:
            return resp.content

    def power_on(self):
        """Power on the box."""
        payload = amt.wsman.power_state_request(self.path, "on")
        self.post(payload, CIM_PowerManagementService)
        return 0

    def power_off(self):
        """Power off the box."""
        payload = amt.wsman.power_state_request(self.path, "off")
        self.post(payload, CIM_PowerManagementService)
        return 0

    def power_cycle(self):
        """Power cycle the box."""
        payload = amt.wsman.power_state_request(self.path, "reboot")
        self.post(payload, CIM_PowerManagementService)
        return 0

    def power_cycle_hard(self):
        """Power cycle hard the box."""
        payload = amt.wsman.power_state_request(self.path, "reset")
        self.post(payload, CIM_PowerManagementService)
        return 0

    def power_sleep(self):
        """Put the box to sleep."""
        payload = amt.wsman.power_state_request(self.path, "sleep")
        self.post(payload, CIM_PowerManagementService)
        return 0

    def power_hibernate(self):
        """Hibernate the box."""
        payload = amt.wsman.power_state_request(self.path, "hibernate")
        self.post(payload, CIM_PowerManagementService)
        return 0

    def pxe_next_boot(self):
        """Sets the machine to PXE boot on its next reboot

        Will default back to normal boot list on the reboot that follows.
        """
        self.set_next_boot(boot_device='pxe')

    def set_next_boot(self, boot_device):
        """Sets the machine to boot to boot_device on its next reboot

        Will default back to normal boot list on the reboot that follows.
        """
        payload = amt.wsman.change_boot_order_request(self.path, boot_device)
        self.post(payload)

        payload = amt.wsman.enable_boot_config_request(self.path)
        self.post(payload)

    def power_status(self):
        payload = amt.wsman.get_request(
            self.path,
            CIM_AssociatedPowerManagementService)
        resp = self.post(payload)
        value = _find_value(
            resp,
            CIM_AssociatedPowerManagementService,
            "PowerState")
        return value

    def get_pki_certs(self):
        certs = self._enum_values(AMT_PublicKeyCertificate)
        return [_xml_to_dict(cert) for cert in certs]

    def add_pki_cert(self, filename, trusted):
        certs = []
        for cert in pem.parse_file(filename):
            content = "".join(cert.as_text().splitlines()[1:-1])
            resp = self.post(amt.wsman.add_cert(self.path, content, trusted))
            rv = _return_value(resp, AMT_PublicKeyManagementService)
            selector = _find_node(resp, _WSMAN, "Selector")
            certs.append((rv, None if selector is None else selector.text))
        return certs

    def remove_pki_cert(self, selector):
        resp = self.post(amt.wsman.delete_item(self.path, AMT_PublicKeyCertificate, "InstanceID", selector))
        return _find_value(resp, _ADDRESS, "Action") == "http://schemas.xmlsoap.org/ws/2004/09/transfer/DeleteResponse"

    def get_pki_keys(self):
        keys = self._enum_values(AMT_PublicPrivateKeyPair)
        return [_xml_to_dict(key) for key in keys]

    def add_pki_key(self, filename):
        keys = []
        for key in pem.parse_file(filename):
            content = "".join(key.as_text().splitlines()[1:-1])
            resp = self.post(amt.wsman.add_key(self.path, content))
            rv = _return_value(resp, AMT_PublicKeyManagementService)
            selector = _find_node(resp, _WSMAN, "Selector")
            keys.append((rv, None if selector is None else selector.text))
        return keys

    def generate_pki_key(self, bits):
        resp = self.post(amt.wsman.generate_key(self.path, bits))
        rv = _return_value(resp, AMT_PublicKeyManagementService)
        selector = _find_node(resp, _WSMAN, "Selector")
        return (rv, None if selector is None else selector.text)

    def sign_pki_csr(self, filename, selector):
        requests = []
        for request in pem.parse_file(filename):
            content = "".join(request.as_text().splitlines()[1:-1])
            resp = self.post(amt.wsman.sign_pki_csr(self.path, content, "InstanceID", selector))
            rv = _return_value(resp, AMT_PublicKeyManagementService)
            signed_request = _find_node(resp, AMT_PublicKeyManagementService, "SignedCertificateRequest")
            requests.append((rv, None if signed_request is None else signed_request.text))
        return requests

    def remove_pki_key(self, selector):
        resp = self.post(amt.wsman.delete_item(self.path, AMT_PublicPrivateKeyPair, "InstanceID", selector))
        return _find_value(resp, _ADDRESS, "Action") == "http://schemas.xmlsoap.org/ws/2004/09/transfer/DeleteResponse"

    def _format_tls_credentials(self, xml_creds):
        creds = {}
        for cred in xml_creds:
            instance = cred.find('./{' + AMT_TLSCredentialContext + '}ElementProvidingContext//{' + _WSMAN + '}Selector')
            if instance is not None:
                instance = instance.text
            creds[instance] = _xml_to_dict(cred)
        return creds

    def get_tls_credentials(self):
        return self._format_tls_credentials(self._enum_values(AMT_TLSCredentialContext))

    def configure_tls_pki(self, instance):
        exists = len(self._enum_values(AMT_TLSCredentialContext)) > 0

        if instance == '':
            if exists:
                self.post(amt.wsman.delete_item(self.path, AMT_TLSCredentialContext, None, None))
            return exists

        creds = amt.wsman.prepare_tls_credentials(instance)

        if exists:
            self.post(amt.wsman.put_item(self.path, AMT_TLSCredentialContext, None, None, creds))
        else:
            self.post(amt.wsman.create_item(self.path, AMT_TLSCredentialContext, creds))

        return True

    def _enable_tls(self, instance, plaintext, mutual, cn):
        resp = self.post(amt.wsman.get_item(self.path, AMT_TLSSettingData, "InstanceID", instance))
        config = _find_node(resp, AMT_TLSSettingData, "AMT_TLSSettingData")
        _xml_set(config, AMT_TLSSettingData, "Enabled", ["true"])
        _xml_set(config, AMT_TLSSettingData, "AcceptNonSecureConnections", plaintext)
        _xml_set(config, AMT_TLSSettingData, "MutualAuthentication", mutual)
        if cn is not None:
            _xml_set(config, AMT_TLSSettingData, "TrustedCN", cn)
        self.post(amt.wsman.put_item(self.path, AMT_TLSSettingData, "InstanceID", instance, config))
        return True

    def enable_remote_tls(self, plaintext, mutual, cn):
        if mutual:
            self.set_time()
        return self._enable_tls(_TLS_REMOTE, [str(plaintext).lower()], [str(mutual).lower()], cn)

    def enable_local_tls(self):
        return self._enable_tls(_TLS_LOCAL, ["true"], ["false"], None)

    def get_tls_status(self):
        types = {
            _TLS_REMOTE: "remote",
            _TLS_LOCAL: "local",
        }
        settings = {}
        for setting in self._enum_values(AMT_TLSSettingData):
            instance = setting.find('./{' + AMT_TLSSettingData + '}InstanceID')
            if instance is not None:
                instance = instance.text
            settings[types.get(instance)] = _xml_to_dict(setting)
        return settings

    def _disable_tls(self, instance):
        resp = self.post(amt.wsman.get_item(self.path, AMT_TLSSettingData, "InstanceID", instance))
        config = _find_node(resp, AMT_TLSSettingData, "AMT_TLSSettingData")
        _xml_set(config, AMT_TLSSettingData, "Enabled", ["false"])
        resp = self.post(amt.wsman.put_item(self.path, AMT_TLSSettingData, "InstanceID", instance, config))
        return _xml_to_dict(_find_node(resp, AMT_TLSSettingData, "AMT_TLSSettingData"))

    def disable_remote_tls(self):
        return self._disable_tls(_TLS_REMOTE)

    def disable_local_tls(self):
        return self._disable_tls(_TLS_LOCAL)

    def commit_setup_changes(self):
        return self.post(amt.wsman.commit_setup_changes(self.path), AMT_SetupAndConfigurationService)

    def set_time(self):
        remote_reference_time = int(time.time())
        resp = self.post(amt.wsman.get_time(self.path))
        local_reference_time = int(_find_value(resp, AMT_TimeSynchronizationService, "Ta0"))

        remote_current_time = int(time.time())
        resp = self.post(amt.wsman.set_time(self.path, local_reference_time, remote_reference_time, remote_current_time))

        return {"old": local_reference_time, "new": remote_current_time, "rv": _return_value(resp, AMT_TimeSynchronizationService)}

    def get_uuid(self):
        resp = self.post(amt.wsman.get_request(self.path, CIM_ComputerSystemPackage))
        value = _find_value(resp, CIM_ComputerSystemPackage, "PlatformGUID")
        return uuid.UUID(value)

    def get_version(self):
        self.get_uuid()
        return self.version

    def enable_vnc(self):
        if self.vncpassword is None:
            print("VNC Password was not set")
            return False
        payload = amt.wsman.enable_remote_kvm(self.path, self.vncpassword)
        self.post(payload)
        payload = amt.wsman.kvm_redirect(self.path)
        self.post(payload)
        return True

    def enable_kvm(self, nodelay=False):
        payload = amt.wsman.enable_remote_kvm(self.path, "", False, nodelay)
        self.post(payload)
        return True

    def start_kvm(self):
        payload = amt.wsman.kvm_redirect(self.path)
        return self.post(payload, CIM_KVMRedirectionSAP) == 0

    def vnc_status(self):
        payload = amt.wsman.get_request(
            self.path,
            ('http://intel.com/wbem/wscim/1/ips-schema/1/'
             'IPS_KVMRedirectionSettingData'))
        return pp_xml(self.post(payload))

    def _enum_values(self, resource):
        values = []

        resp = self.post(amt.wsman.enumerate_begin(self.path, resource))
        context = _find_value(resp, _SOAP_ENUMERATION, "EnumerationContext")

        while context:
            resp = self.post(amt.wsman.enumerate_next(self.path, resource, context))
            items = _find_node(resp, _SOAP_ENUMERATION, "Items")
            for item in items:
                values.append(item)

            eos = _find_node(resp, _SOAP_ENUMERATION, "EndOfSequence")
            if eos is not None:
                context = None

        return values


def _find_node(content, ns, key):
    """Find the return value in a response."""
    doc = ElementTree.fromstring(content)
    query = './/{%(ns)s}%(item)s' % {'ns': ns, 'item': key}
    return doc.find(query)


def _find_value(content, ns, key):
    """Find the return value in a CIM response.

    The xmlns is needed because everything in CIM is a million levels
    of namespace indirection.
    """
    return _find_node(content, ns, key).text


def _return_value(content, ns):
    """Find the return value in a CIM response.

    The xmlns is needed because everything in CIM is a million levels
    of namespace indirection.
    """
    rv = _find_node(content, ns, 'ReturnValue')
    return None if rv is None else int(rv.text)


def _xml_to_dict(elements):
    data = {}
    for key, value in [(element.tag.rpartition("}")[2], (_xml_to_dict(element) if element else element.text)) for element in elements]:
        if key in data:
            if type(data[key]) != list:
                data[key] = [data[key]]
            data[key].append(value)
        else:
            data[key] = value
    return data


def _xml_set(elements, ns, tag, values):
    for element in elements.findall('./{%(ns)s}%(item)s' % {'ns': ns, 'item': tag}):
        elements.remove(element)
    for value in values:
        element = ElementTree.SubElement(elements, '{%(ns)s}%(item)s' % {'ns': ns, 'item': tag})
        element.text = value


class KVM(object):
    def __init__(self, client, filename):
        self.client = client
        self.filename = filename

    def _unlink(self):
        try:
            if stat.S_ISSOCK(os.lstat(self.filename).st_mode):
                os.unlink(self.filename)
        except FileNotFoundError:
            pass

    def __enter__(self):
        self._unlink()
        rv = self.client.start_kvm()
        assert rv
        self.incoming = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
        self.incoming.bind(self.filename)
        self.incoming.listen()
        self.incoming.setblocking(False)
        return self

    def loop(self):
        while True:
            rlist, _, _ = select.select([self.incoming], [], [self.incoming])
            if self.incoming in rlist:
                (conn, _) = self.incoming.accept()

                pid = os.fork()
                if pid == 0:
                    self.incoming.close()
                    self.incoming = None

                    with KVMClient(self, conn) as kvm:
                        kvm.start()
                        kvm.loop()
                    return
                else:
                    assert pid > 0
                    conn.close()
            else:
                return

    def __exit__(self, type, value, traceback):
        if self.incoming is not None:
            self.incoming.close()
            self._unlink()
        return False


class KVMClient(object):
    def __init__(self, kvm, vnc):
        self.kvm = kvm
        self.vnc = vnc
        self.amt = None
        self.tls = None
        self._want_write = False
        if self.kvm.client.protocol == "https":
            self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            if self.kvm.client.session.verify != False:
                self.context.load_verify_locations(self.kvm.client.session.verify)
            if self.kvm.client.session.cert:
                self.context.load_cert_chain(self.kvm.client.session.cert[0], self.kvm.client.session.cert[1])

    def __enter__(self):
        return self

    def start(self):
        def hex_md5(data):
            return hashlib.md5(data).hexdigest().encode("us-ascii")

        print("Connection starting")
        self.amt = socket.create_connection((self.kvm.client.address, self.kvm.client.port + 2))
        if self.kvm.client.protocol == "https":
            self.tls = self.context.wrap_socket(self.amt, server_hostname=self.kvm.client.address)
        else:
            self.tls = self.amt

        # StartRedirectionSession
        self.tls.send(b"\x10\x01\x00\x00KVMR")

        # StartRedirectionSessionReply
        data = self.tls.recv(1)
        assert data == b"\x11", data
        data = self.tls.recv(1)
        assert data == b"\x00", data # Status
        data = self.tls.recv(11)
        assert len(data) == 11, data

        # Query for available authentication
        self.tls.send(b"\x13\x00\x00\x00\x00\x00\x00\x00\x00")

        # AuthenticateSessionReply
        data = self.tls.recv(1)
        assert data == b"\x14", data
        data = self.tls.recv(1)
        assert data == b"\x00", data # Status
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(1)
        assert data == b"\x00", data # Auth type
        data = self.tls.recv(4)
        assert len(data) == 4
        length = data[0] | (data[1] >> 8) | (data[2] >> 16) | (data[3] >> 24)
        assert length > 0, length
        data = self.tls.recv(length)
        assert len(data) == length
        assert b"\x04" in data, data # Digest auth

        # Digest auth
        user = self.kvm.client.username.encode("us-ascii")
        path = b"/RedirectionService"
        content = bytes([len(user)]) + user + b"\x00\x00" + bytes([len(path)]) + path + b"\x00\x00\x00\x00"
        length = len(content)
        length = bytes([length & 0xFF, (length >> 8) & 0xFF, (length >> 16) & 0xFF, (length >> 24) & 0xFF])
        self.tls.send(b"\x13\x00\x00\x00\x04" + length + content)

        # AuthenticateSessionReply
        data = self.tls.recv(1)
        assert data == b"\x14", data
        data = self.tls.recv(1)
        assert data == b"\x01", data # Status
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(1)
        assert data == b"\x04", data # Auth type (digest)
        data = self.tls.recv(4)
        assert len(data) == 4
        length = data[0] | (data[1] >> 8) | (data[2] >> 16) | (data[3] >> 24)
        assert length >= 3, length
        data = self.tls.recv(length)
        assert len(data) == length, (data, length)

        pos = 0
        realm_len = data[pos]
        realm = data[pos+1:pos+1+realm_len]

        pos += realm_len + 1
        nonce_len = data[pos]
        nonce = data[pos+1:pos+1+nonce_len]
        pos += nonce_len + 1

        qop_len = data[pos]
        qop = data[pos+1:pos+1+qop_len]
        pos += qop_len + 1

        snc = b"00000002"
        cnonce = secrets.token_hex(16).encode("us-ascii")
        extra = snc + b":" + cnonce + b":" + qop + b":"
        digest = hex_md5(hex_md5(user + b":" + realm + b":" + self.kvm.client.password.encode("us-ascii"))
                              + b":" + nonce + b":" + extra + hex_md5(b"POST:" + path))
        content = (bytes([len(user)]) + user + bytes([realm_len]) + realm + bytes([nonce_len]) + nonce + bytes([len(path)]) + path
            + bytes([len(cnonce)]) + cnonce + bytes([len(snc)]) + snc + bytes([len(digest)]) + digest + bytes([qop_len]) + qop)
        length = len(content)
        length = bytes([length & 0xFF, (length >> 8) & 0xFF, (length >> 16) & 0xFF, (length >> 24) & 0xFF])
        self.tls.send(b"\x13\x00\x00\x00\x04" + length + content)

        # AuthenticateSessionReply
        data = self.tls.recv(1)
        assert data == b"\x14", data
        data = self.tls.recv(1)
        assert data == b"\x00", data # Status
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(1)
        assert data == b"\x04", data # Auth type (digest)
        data = self.tls.recv(4)
        assert len(data) == 4
        length = data[0] | (data[1] >> 8) | (data[2] >> 16) | (data[3] >> 24)
        if length > 0:
            data = self.tls.recv(length)
            assert len(data) == length, (data, length)

        # Remote Desktop
        self.tls.send(b"\x40\x00\x00\x00\x00\x00\x00\x00")

        data = self.tls.recv(1)
        assert data == b"\x41", data
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(1)
        assert data == b"\x00", data
        data = self.tls.recv(4)
        assert len(data) == 4
        length = data[0] | (data[1] >> 8) | (data[2] >> 16) | (data[3] >> 24)
        if length > 0:
            data = self.tls.recv(length)
            assert len(data) == length, (data, length)

        print("Connection started")
        self.tls.setblocking(False)
        self.vnc.setblocking(False)
        self.running = True

    def _read_tls(self):
        try:
            data = self.tls.recv(4096)
            if len(data) == 0:
                print("Server closed connection")
                self.running = False
                return
            self._want_write = False
            self.vnc.setblocking(True)
            self.vnc.send(data)
            self.vnc.setblocking(False)
        except ssl.SSLWantReadError:
            pass
        except ssl.SSLWantWriteError:
            self._want_write = True

    def _read_vnc(self):
        data = self.vnc.recv(4096)
        if len(data) == 0:
            print("Client closed connection")
            self.running = False
            return
        self.tls.setblocking(True)
        self.tls.send(data)
        self.tls.setblocking(False)

    def loop(self):
        while self.running:
            self._read_tls()
            rlist, wlist, _ = select.select([self.vnc, self.tls], [self.tls] if self._want_write else [], [])
            if self.tls in rlist or (self._want_write and self.tls in wlist):
                self._read_tls()
            if self.vnc in rlist:
                self._read_vnc()

    def __exit__(self, type, value, traceback):
        self.vnc.close()
        if self.tls is not None:
            self.tls.close()
        if self.amt is not None:
            self.amt.close()
        return False