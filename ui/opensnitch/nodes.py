from queue import Queue
from datetime import datetime
import time
import json

from database import Database

class Nodes():
    __instance = None
    LOG_TAG = "[Nodes]: "
    ONLINE = "\u2713 online"
    OFFLINE = "\u2613 offline"
    WARNING = "\u26a0"

    @staticmethod
    def instance():
        if Nodes.__instance == None:
            Nodes.__instance = Nodes()
        return Nodes.__instance

    def __init__(self):
        self._db = Database.instance()
        self._nodes = {}
        self._notifications_sent = {}

    def count(self):
        return len(self._nodes)

    def add(self, context, client_config=None):
        try:
            proto, _addr = self.get_addr(context.peer())
            addr = "%s:%s" % (proto, _addr)
            if addr not in self._nodes:
                self._nodes[addr] = {
                        'notifications': Queue(),
                        'online':        True,
                        'last_seen':     datetime.now()
                        }
                self.add_data(addr, client_config)
                return self._nodes[addr]

            self._nodes[addr]['last_seen'] = datetime.now()
            self.add_data(addr, client_config)
            self._nodes.update(proto, addr)

            return self._nodes[addr]

        except Exception as e:
            print(self.LOG_TAG + " exception adding/updating node: ", e, addr, client_config)

        return None

    def add_data(self, addr, client_config):
        if client_config != None:
            self._nodes[addr]['data'] = self.get_client_config(client_config)
            self.add_rules(addr, client_config.rules)

    def add_rules(self, addr, rules):
        try:
            for _,r in enumerate(rules):
                self._db.insert("rules",
                        "(time, node, name, enabled, precedence, action, duration, operator_type, operator_sensitive, operator_operand, operator_data)",
                            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                addr,
                                r.name, str(r.enabled), str(r.precedence), r.action, r.duration,
                                r.operator.type,
                                str(r.operator.sensitive),
                                r.operator.operand,
                                r.operator.data),
                            action_on_conflict="IGNORE")
        except Exception as e:
            print(self.LOG_TAG + " exception adding node to db: ", e)

    def delete_all(self):
        self.send_notifications(None)
        self._nodes = {}

    def delete(self, peer):
        proto, addr = self.get_addr(peer)
        addr = "%s:%s" % (proto, addr)
        # Force the node to get one new item from queue,
        # in order to loop and exit.
        self._nodes[addr]['notifications'].put(None)
        if addr in self._nodes:
            del self._nodes[addr]

    def get(self):
        return self._nodes

    def get_node(self, addr):
        try:
            return self._nodes[addr]
        except Exception as e:
            return None

    def get_nodes(self):
        return self._nodes

    def get_node_config(self, addr):
        try:
            return self._nodes[addr]['data'].config
        except Exception as e:
            print(self.LOG_TAG + " exception get_node_config(): ", e)
            return None

    def get_client_config(self, client_config):
        try:
            node_config = json.loads(client_config.config)
            if 'LogLevel' not in node_config:
                node_config['LogLevel'] = 1
                client_config.config = json.dumps(node_config)
        except Exception as e:
            print(self.LOG_TAG, "exception parsing client config", e)

        return client_config

    def get_addr(self, peer):
        peer = peer.split(":")
        # WA for backward compatibility
        if peer[0] == "unix" and peer[1] == "":
            peer[1] = "local"
        return peer[0], peer[1]

    def get_notifications(self):
        notlist = []
        try:
            for c in self._nodes:
                if self._nodes[c]['online'] == False:
                    continue
                if self._nodes[c]['notifications'].empty():
                    continue
                notif = self._nodes[c]['notifications'].get(False)
                if notif != None:
                    self._nodes[c]['notifications'].task_done()
                    notlist.append(notif)
        except Exception as e:
            print(self.LOG_TAG + " exception get_notifications(): ", e)

        return notlist

    def save_node_config(self, addr, config):
        try:
            self._nodes[addr]['data'].config = config
        except Exception as e:
            print(self.LOG_TAG + " exception saving node config: ", e, addr, config)

    def save_nodes_config(self, config):
        try:
            for c in self._nodes:
                self._nodes[c]['data'].config = config
        except Exception as e:
            print(self.LOG_TAG + " exception saving nodes config: ", e, config)

    def send_notification(self, addr, notification, callback_signal=None):
        try:
            notification.id = int(str(time.time()).replace(".", ""))
            self._nodes[addr]['notifications'].put(notification)
            self._notifications_sent[notification.id] = callback_signal
        except Exception as e:
            print(self.LOG_TAG + " exception sending notification: ", e, addr, notification)

        return notification.id

    def send_notifications(self, notification, callback_signal=None):
        """
        Enqueues a notification to the clients queue.
        It'll be retrieved and delivered by get_notifications
        """
        try:
            notification.id = int(str(time.time()).replace(".", ""))
            for c in self._nodes:
                self._nodes[c]['notifications'].put(notification)
            self._notifications_sent[notification.id] = callback_signal
        except Exception as e:
            print(self.LOG_TAG + " exception sending notifications: ", e, notification)

        return notification.id

    def reply_notification(self, reply):
        if reply == None:
            print(self.LOG_TAG, " reply notification None")
            return
        if reply.id in self._notifications_sent:
            if self._notifications_sent[reply.id] != None:
                self._notifications_sent[reply.id].emit(reply)
            del self._notifications_sent[reply.id]

    def update(self, proto, addr, status=ONLINE):
        try:
            self._db.update("nodes",
                    "hostname=?,version=?,last_connection=?,status=? WHERE addr=?",
                    (
                        self._nodes[proto+":"+addr]['data'].name,
                        self._nodes[proto+":"+addr]['data'].version,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        status,
                        addr)
                    )
        except Exception as e:
            print(self.LOG_TAG + " exception updating DB: ", e, addr)
