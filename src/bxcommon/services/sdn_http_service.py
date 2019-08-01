import time
from typing import List, Optional

from bxcommon.constants import SdnRoutes
from bxcommon.models.blockchain_network_model import BlockchainNetworkModel
from bxcommon.models.node_event_model import NodeEventModel, NodeEventType
from bxcommon.models.node_model import NodeModel
from bxcommon.models.outbound_peer_model import OutboundPeerModel
from bxcommon.services import http_service
from bxcommon.utils import config, logger, model_loader, json_utils


# TODO port this to sockets soon and remove json serialization perf hit on the node.


def fetch_node_attributes(node_id):
    # Should only be used for test networks.
    node_url = SdnRoutes.node.format(node_id)
    opts = http_service.get_json(node_url)
    logger.debug("Retrieved config for id {0} : {1}".format(node_id, opts))

    if opts:
        return model_loader.load(NodeModel, opts)
    else:
        return None


def _fetch_peers(node_url, node_id=None):
    outbound_peers = http_service.get_json(node_url)
    logger.trace("Retrieved outbound peers for node {0} from endpoint {1}: {2}"
                 .format(node_id, node_url, outbound_peers))

    if not outbound_peers:
        logger.warn("Got no outbound peers for endpoint: {}".format(node_url))
        return []

    outbound_peers = [model_loader.load(OutboundPeerModel, o) for o in outbound_peers]
    config.blocking_resolve_peers(outbound_peers)
    return outbound_peers


def fetch_potential_relay_peers(node_id: str) -> Optional[List[OutboundPeerModel]]:
    node_url = SdnRoutes.node_potential_relays.format(node_id)
    return _fetch_peers(node_url, node_id)


def fetch_potential_relay_peers_by_network(node_id: str, network_num: int) -> Optional[List[OutboundPeerModel]]:
    node_url = SdnRoutes.node_potential_relays_by_network.format(node_id, network_num)
    return _fetch_peers(node_url, node_id)


def fetch_gateway_peers(node_id):
    node_url = SdnRoutes.node_gateways.format(node_id)
    return _fetch_peers(node_url, node_id)


def fetch_remote_blockchain_peer(network_num):
    node_url = SdnRoutes.node_remote_blockchain.format(network_num)
    peers = _fetch_peers(node_url)
    if len(peers) != 1:
        logger.warn("Did not get expected number of peers from SDN.")
        return None
    else:
        return peers[0]


def fetch_blockchain_network(protocol_name, network_name):
    node_url = SdnRoutes.blockchain_network.format(protocol_name, network_name)
    blockchain_network = http_service.get_json(node_url)

    if blockchain_network is None:
        return None

    blockchain_network = model_loader.load(BlockchainNetworkModel, blockchain_network)

    return blockchain_network


def fetch_blockchain_networks():
    node_url = SdnRoutes.blockchain_networks
    blockchain_networks = http_service.get_json(node_url)

    if not blockchain_networks:
        logger.warn("There are no blockchain networks configured in SDN")
        return []

    blockchain_networks = [model_loader.load(BlockchainNetworkModel, b) for b in blockchain_networks]

    return blockchain_networks


def submit_sid_space_full_event(node_id):
    submit_node_event(NodeEventModel(node_id=node_id, event_type=NodeEventType.SID_SPACE_FULL))


def submit_node_online_event(node_id):
    submit_node_event(NodeEventModel(node_id=node_id, event_type=NodeEventType.ONLINE))


def submit_node_offline_event(node_id):
    submit_node_event(NodeEventModel(node_id=node_id, event_type=NodeEventType.OFFLINE))


def submit_peer_connection_error_event(node_id, peer_ip, peer_port):
    submit_peer_connection_event(NodeEventType.PEER_CONN_ERR, node_id, peer_ip, peer_port)


def submit_peer_connection_event(event_type, node_id, peer_ip, peer_port):
    submit_node_event(
        NodeEventModel(node_id=node_id, event_type=event_type, peer_ip=peer_ip, peer_port=peer_port))


def submit_gateway_inbound_connection(node_id, peer_id):
    http_service.post_json(SdnRoutes.gateway_inbound_connection.format(node_id), peer_id)


def submit_sync_txs_event(node_id):
    submit_node_event(NodeEventModel(node_id=node_id, event_type=NodeEventType.TX_SERVICE_FULLY_SYNCED))


def submit_node_txs_sync_in_network(node_id, networks: List[int]):
    submit_node_event(NodeEventModel(node_id=node_id, event_type=NodeEventType.TX_SERVICE_SYNCED_IN_NETWORK, tx_sync_networks=networks))


def delete_gateway_inbound_connection(node_id, peer_id):
    http_service.delete_json(SdnRoutes.gateway_inbound_connection.format(node_id), peer_id)


def submit_node_event(node_event_model):
    node_event_model.timestamp = str(time.time())
    logger.debug("Submitting event for node {0} {1}"
                 .format(node_event_model.node_id, json_utils.serialize(node_event_model)))
    url = SdnRoutes.node_event.format(node_event_model.node_id)
    http_service.post_json(url, node_event_model)


def register_node(node_model):
    if not node_model:
        raise ValueError("Missing node model.")

    # Let the SDN know who this is.
    # SDN determines peers and returns full config.
    node_config = http_service.post_json(SdnRoutes.nodes, node_model)

    logger.debug("Registered node. Response: {}".format(json_utils.serialize(node_config)))

    if not node_config:
        raise EnvironmentError("Unable to reach SDN and register this node. Please check connection.")

    return model_loader.load(NodeModel, node_config)
