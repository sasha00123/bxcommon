import ConfigParser
import socket

import time

# Some websites are blocked in certain jurisdictions, so we try multiple websites to see whichever one works.
WEBSITES_TO_TRY = ['www.google.com', 'www.alibaba.com']


# Returns the local internal IP address of the node.
# If the node is behind a NAT or proxy, then this is not the externally visible IP address.
def get_my_ip():
    for website in WEBSITES_TO_TRY:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((website, 80))
            return s.getsockname()[0]
        except socket.timeout:
            continue

    raise Exception("Could not find any local name!")


# Parse the config filename and return a params dictionary with the params from ALL_PARAMS
def parse_config_file(filename, localname, params):
    client_config = ConfigParser.ConfigParser()
    client_config.read(filename)

    config_params = {}
    for param_name in params:
        config_params[param_name] = getparam(client_config, localname, param_name)

    return client_config, config_params


# Gets the param "pname" from the config file.
# If the param exists under the localname, we use that one. Otherwise, we use
# the param under default.
def getparam(client_config, local_name, param_name):
    if not client_config:
        return None

    try:
        return client_config.get(local_name, param_name)
    except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
        try:
            return client_config.get("default", param_name)
        except (ConfigParser.NoOptionError, ConfigParser.NoSectionError):
            return None


# Parse the peers file and returns a dictionary of {cls : list of ip, port pairs}
# to tell the node which connection types to instantiate.
# Parse the peers string and return two lists:
#   1) A list of relays that are internal nodes in our network
#   2) A list of trusted peers that we will connect to.
def parse_peers(peers_string):
    nodes = {}

    if peers_string is not None:
        for line in peers_string.split(","):
            peers = line.strip().split()

            peer_ip = None
            while peer_ip is None:
                try:
                    peer_ip = socket.gethostbyname(peers[0])
                except socket.error:
                    print "Caught socket error while resolving name! Retrying..."
                    time.sleep(0.1)
                    peer_ip = None

            peer_port = int(peers[1])
            peer_idx = int(peers[2])
            nodes[peer_idx] = (peer_ip, peer_port)

    return nodes
