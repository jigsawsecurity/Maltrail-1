import socket
import struct
import urllib
import re
import os
import urlparse

from core.settings import config
from core.settings import trails
from core.settings import SUSPICIOUS_CONTENT_TYPES
from core.settings import SUSPICIOUS_HTTP_REQUEST_FORCE_ENCODE_CHARS
from core.settings import WHITELIST_HTTP_REQUEST_PATHS
from core.settings import SUSPICIOUS_HTTP_REQUEST_PRE_CONDITION
from core.settings import SUSPICIOUS_UA_REGEX
from core.settings import SUSPICIOUS_HTTP_REQUEST_REGEXES
from core.settings import SUSPICIOUS_DIRECT_DOWNLOAD_EXTENSIONS
from core.settings import WEB_SHELLS
from core.settings import WHITELIST_DIRECT_DOWNLOAD_KEYWORDS
from core.settings import SUSPICIOUS_HTTP_PATH_REGEXES
from core.settings import WHITELIST_UA_KEYWORDS
from core.process_package import result_cache
from core.check_domain import check_domain_whitelisted
from core.enums import PROTO
from core.enums import TRAIL
from core.log import log_event

# TODO: Split this across multiple plugins

def plugin(pkg):
    if pkg.protocol == socket.IPPROTO_TCP:
        src_port, dst_port, _, _, doff_reserved, flags = struct.unpack("!HHLLBB", pkg.ip_data[pkg.iph_length:pkg.iph_length+14])

        if flags != 2:
            tcph_length = doff_reserved >> 4
            h_size = pkg.iph_length + (tcph_length << 2)
            tcp_data = pkg.ip_data[h_size:]

            if tcp_data.startswith("HTTP/"):
                if any(_ in tcp_data[:tcp_data.find("\r\n\r\n")] for _ in ("X-Sinkhole:", "X-Malware-Sinkhole:", "Server: You got served", "Server: Apache 1.0/SinkSoft", "sinkdns.org")) or "\r\n\r\nsinkhole" in tcp_data:
                    log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.IP, pkg.src_ip, "sinkhole response (malware)", "(heuristic)"))
                else:
                    index = tcp_data.find("<title>")
                    if index >= 0:
                        title = tcp_data[index + len("<title>"):tcp_data.find("</title>", index)]
                        if all(_ in title.lower() for _ in ("this domain", "has been seized")):
                            log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.IP, title, "seized domain (suspicious)", "(heuristic)"))

                content_type = None
                first_index = tcp_data.find("\r\nContent-Type:")
                if first_index >= 0:
                    first_index = first_index + len("\r\nContent-Type:")
                    last_index = tcp_data.find("\r\n", first_index)
                    if last_index >= 0:
                        content_type = tcp_data[first_index:last_index].strip().lower()

                if content_type and content_type in SUSPICIOUS_CONTENT_TYPES:
                    log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.HTTP, content_type, "content type (suspicious)", "(heuristic)"))

            method, path = None, None
            index = tcp_data.find("\r\n")
            if index >= 0:
                line = tcp_data[:index]
                if line.count(' ') == 2 and " HTTP/" in line:
                    method, path, _ = line.split(' ')

            if method and path:
                post_data = None
                host = pkg.dst_ip
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
                        if host and host[0].isalpha() and pkg.dst_ip in trails:
                            log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.IP, "%s (%s)" % (pkg.dst_ip, host.split(':')[0]), trails[pkg.dst_ip][0], trails[pkg.dst_ip][1]))
                elif config.USE_HEURISTICS and config.CHECK_MISSING_HOST:
                    log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.HTTP, "%s%s" % (host, path), "missing host header (suspicious)", "(heuristic)"))

                index = tcp_data.find("\r\n\r\n")
                if index >= 0:
                    post_data = tcp_data[index + 4:]
                
                if "://" in path:
                    url = path.split("://", 1)[1]

                    if '/' not in url:
                        url = "%s/" % url
                        
                    host, path = url.split('/', 1)
                    
                    if host.endswith(":80"):
                        host = host[:-3]

                    path = "/%s" % path
                elif method == "CONNECT":
                    if '/' in path:
                        host, path = path.split('/', 1)
                        path = "/%s" % path
                    else:
                        host, path = path, '/'
                    
                    if host.endswith(":80"):
                        host = host[:-3]
                    url = "%s%s" % (host, path)
                else:
                    url = "%s%s" % (host, path)

                if config.USE_HEURISTICS:
                    user_agent, result = None, None

                    first_index = tcp_data.find("\r\nUser-Agent:")
                    if first_index >= 0:
                        first_index = first_index + len("\r\nUser-Agent:")
                        last_index = tcp_data.find("\r\n", first_index)
                        if last_index >= 0:
                            user_agent = tcp_data[first_index:last_index]
                            user_agent = urllib.unquote(user_agent).strip()

                    if user_agent:
                        result = result_cache.get(user_agent)
                        if result is None:
                            if not any(_ in user_agent for _ in WHITELIST_UA_KEYWORDS):
                                match = re.search(SUSPICIOUS_UA_REGEX, user_agent)
                                if match:
                                    def _(value):
                                        return value.replace('(', "\\(").replace(')', "\\)")

                                    parts = user_agent.split(match.group(0), 1)

                                    if len(parts) > 1 and parts[0] and parts[-1]:
                                        result = result_cache[user_agent] = "%s (%s)" % (
                                            _(match.group(0)), _(user_agent))
                                    else:
                                        result = result_cache[user_agent] = _(match.group(0)).join(
                                            ("(%s)" if part else "%s") % _(part) for part in parts)
                            if not result:
                                result_cache[user_agent] = False
                        else:
                            log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.UA, result, "user agent (suspicious)", "(heuristic)"))

                if not check_domain_whitelisted(host):
                    checks = [path.rstrip('/')]
                    if '?' in path:
                        checks.append(path.split('?')[0].rstrip('/'))

                    _ = os.path.splitext(checks[-1])
                    if _[1]:
                        checks.append(_[0])

                    if checks[-1].count('/') > 1:
                        checks.append(checks[-1][:checks[-1].rfind('/')])
                        checks.append(
                            checks[0][checks[0].rfind('/'):].split('?')[0])

                    for check in filter(None, checks):
                        for _ in ("", host):
                            check = "%s%s" % (_, check)
                            if check in trails:
                                parts = url.split(check)
                                other = ("(%s)" % _ if _ else _ for _ in parts)
                                trail = check.join(other)
                                log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.URL, trail, trails[check][0], trails[check][1]))
                                return

                    if "%s/" % host in trails:
                        trail = "%s/" % host
                        log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.URL, trail, trails[trail][0], trails[trail][1]))
                        return

                    if config.USE_HEURISTICS:
                        unquoted_path = urllib.unquote(path)
                        unquoted_post_data = urllib.unquote(post_data or "")
                        for char in SUSPICIOUS_HTTP_REQUEST_FORCE_ENCODE_CHARS:
                            replacement = SUSPICIOUS_HTTP_REQUEST_FORCE_ENCODE_CHARS[char]
                            path = path.replace(char, replacement)
                            if post_data:
                                post_data = post_data.replace(
                                    char, replacement)

                        if not any(_ in unquoted_path.lower() for _ in WHITELIST_HTTP_REQUEST_PATHS):
                            if any(_ in unquoted_path for _ in SUSPICIOUS_HTTP_REQUEST_PRE_CONDITION):
                                found = result_cache.get(unquoted_path)
                                if found is None:
                                    for desc, regex in SUSPICIOUS_HTTP_REQUEST_REGEXES:
                                        if re.search(regex, unquoted_path, re.I | re.DOTALL):
                                            found = desc
                                            break
                                    result_cache[unquoted_path] = found or ""
                                if found:
                                    trail = "%s(%s)" % (host, path)
                                    log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.URL, trail, "%s (suspicious)" % found, "(heuristic)"))
                                    return

                            if any(_ in unquoted_post_data for _ in SUSPICIOUS_HTTP_REQUEST_PRE_CONDITION):
                                found = result_cache.get(unquoted_post_data)
                                if found is None:
                                    for desc, regex in SUSPICIOUS_HTTP_REQUEST_REGEXES:
                                        if re.search(regex, unquoted_post_data, re.I | re.DOTALL):
                                            found = desc
                                            break
                                    result_cache[unquoted_post_data] = found or ""
                                if found:
                                    trail = "%s(%s \(%s %s\))" % (host, path, method, post_data.strip())
                                    log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.HTTP, trail, "%s (suspicious)" % found, "(heuristic)"))
                                    return

                        if '.' in path:
                            _ = urlparse.urlparse("http://%s" % url)  # dummy scheme
                            path = path.lower()
                            filename = _.path.split('/')[-1]
                            name, extension = os.path.splitext(filename)
                            trail = "%s(%s)" % (host, path)
                            if extension and extension in SUSPICIOUS_DIRECT_DOWNLOAD_EXTENSIONS and not any(_ in path for _ in WHITELIST_DIRECT_DOWNLOAD_KEYWORDS) and '=' not in _.query and len(name) < 10:
                                log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.URL, trail, "direct %s download (suspicious)" % extension, "(heuristic)"))
                            elif filename in WEB_SHELLS:
                                log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.URL, trail, "potential web shell (suspicious)", "(heuristic)"))
                            else:
                                for desc, regex in SUSPICIOUS_HTTP_PATH_REGEXES:
                                    if re.search(regex, filename, re.I):
                                        log_event((pkg.sec, pkg.usec, pkg.src_ip, src_port, pkg.dst_ip, dst_port, PROTO.TCP, TRAIL.URL, trail, "%s (suspicious)" % desc, "(heuristic)"))
                                        break
