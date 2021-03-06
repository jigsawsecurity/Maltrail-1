import re
import urlparse
import socket

from core.cache import result_cache
from core.trails.check_domain import check_domain_whitelisted
from core.settings import VALID_DNS_CHARS
from core.settings import SUSPICIOUS_DOMAIN_LENGTH_THRESHOLD
from core.settings import WHITELIST_LONG_DOMAIN_NAME_KEYWORDS
from core.enums import TRAIL
from core.events.Event import Event
from core.events.Event import SEVERITY

def _check_domain(query, packet, config, trails):
    if query:
        query = query.lower()
        if ':' in query:
            query = query.split(':', 1)[0]

    if query.replace('.', "").isdigit():  # IP address
        return

    if result_cache.get(query) == False:
        return
    
    if not check_domain_whitelisted(query) and all(_ in VALID_DNS_CHARS for _ in query):
        parts = query.lower().split('.')

        for i in xrange(0, len(parts)):
            domain = '.'.join(parts[i:])
            if domain in trails:
                if domain == query:
                    trail = domain
                else:
                    _ = ".%s" % domain
                    trail = "(%s)%s" % (query[:-len(_)], _)

                if not (re.search(r"(?i)\Ad?ns\d*\.", query) and any(_ in trails.get(domain, " ")[0] for _ in ("suspicious", "sinkhole"))):  # e.g. ns2.nobel.su
                    return Event(packet, TRAIL.DNS, trail, trails[domain][0], trails[domain][1], accuracy=100, severity=SEVERITY.MEDIUM)

        if config.USE_HEURISTICS:
            if len(parts[0]) > SUSPICIOUS_DOMAIN_LENGTH_THRESHOLD and '-' not in parts[0]:
                trail = None

                if len(parts) > 2:
                    trail = "(%s).%s" % ('.'.join(parts[:-2]), '.'.join(parts[-2:]))
                elif len(parts) == 2:
                    trail = "(%s).%s" % (parts[0], parts[1])
                else:
                    trail = query

                if trail and not any(_ in trail for _ in WHITELIST_LONG_DOMAIN_NAME_KEYWORDS):
                    return Event(packet, TRAIL.DNS, trail, "long domain (suspicious)", "(heuristic)", accuracy=75, severity=SEVERITY.VERY_LOW)

    result_cache[query] = False

def plugin(packet, config, trails):
    if packet.ip.get_ip_p() == socket.IPPROTO_TCP:
        tcp_header = packet.ip.child()
        flags = tcp_header.get_th_flags()

        if flags != 2:
            tcp_data = tcp_header.get_data_as_string()
            method, path = None, None
            dst_ip = packet.ip.get_ip_dst()
            dst_port = tcp_header.get_th_dport()

            index = tcp_data.find("\r\n")
            if index >= 0:
                line = tcp_data[:index]
                if line.count(' ') == 2 and " HTTP/" in line:
                    method, path, _ = line.split(' ')
            
            if method and path:
                host = dst_ip
                first_index = tcp_data.find("\r\nHost:")
                path = path.lower()

                if first_index >= 0:
                    first_index = first_index + len("\r\nHost:")
                    last_index = tcp_data.find("\r\n", first_index)
                    if last_index >= 0:
                        host = tcp_data[first_index:last_index]
                        host = host.strip().lower()
                        if host.endswith(":80"):
                            host = host[:-3]
                        
                        if not (host and host[0].isalpha() and dst_ip in trails):
                            _check_domain(host, packet, config, trails)

                if config.USE_HEURISTICS and dst_port == 80 and path.startswith("http://") and not check_domain_whitelisted(urlparse.urlparse(path).netloc.split(':')[0]):
                    trail = re.sub(r"(http://[^/]+/)(.+)", r"\g<1>(\g<2>)", path)
                    return Event(packet, TRAIL.HTTP, trail, "potential proxy probe (suspicious)", "(heuristic)", accuracy=50, severity=SEVERITY.VERY_LOW)
                elif "://" in path:
                    url = path.split("://", 1)[1]

                    if '/' not in url:
                        url = "%s/" % url

                    host, path = url.split('/', 1)
                    if host.endswith(":80"):
                        host = host[:-3]
                    path = "/%s" % path
                    proxy_domain = host.split(':')[0]

                    return _check_domain(proxy_domain, packet, config, trails)
                elif method == "CONNECT":
                    if '/' in path:
                        host, path = path.split('/', 1)
                        path = "/%s" % path
                    else:
                        host, path = path, '/'
                    if host.endswith(":80"):
                        host = host[:-3]
                    proxy_domain = host.split(':')[0]

                    return _check_domain(proxy_domain, packet, config, trails)
