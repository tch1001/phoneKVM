/*
 * hidbridge — owns the USB HID gadget nodes and lets ordinary apps drive them.
 *
 * Why this exists: /dev/hidg* is a root-only character device and Android does
 * not let the untrusted_app domain open it — an SELinux allow rule alone is
 * not sufficient. Rather than punch a hole for every app, this daemon runs as
 * root in its own domain, holds the nodes, and speaks a tiny binary protocol on
 * 127.0.0.1:7700, so app development needs no root, no reboot and no ROM build.
 *
 * Wire format (one report per message, fire and forget):
 *
 *   'K' + 8 bytes   keyboard report -> /dev/hidg0
 *   'M' + 4 bytes   mouse report    -> /dev/hidg1
 *   'P'             ping            -> "OK\n"
 *
 * The payload is byte-for-byte what the gadget descriptors declare, so callers
 * send exactly what they would have written to the node.
 */

#define LOG_TAG "hidbridge"

#include <arpa/inet.h>
#include <fcntl.h>
#include <log/log.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <mutex>
#include <thread>

namespace {

constexpr unsigned short kPort = 7700;
constexpr const char* kKbdPath = "/dev/hidg0";
constexpr const char* kMousePath = "/dev/hidg1";

std::mutex g_lock;
int g_kbd_fd = -1;
int g_mouse_fd = -1;

bool writeReport(int* fd, const char* path, const unsigned char* buf, size_t len) {
  if (*fd < 0) {
    *fd = open(path, O_WRONLY | O_CLOEXEC);
    if (*fd < 0) return false;  // host not attached yet
  }
  ssize_t n = write(*fd, buf, len);
  if (n != static_cast<ssize_t>(len)) {
    // Unplugged mid-write: drop the fd so the next report reopens it.
    ALOGW("write %s: %s", path, strerror(errno));
    close(*fd);
    *fd = -1;
    return false;
  }
  return true;
}

bool readFully(int fd, unsigned char* buf, size_t len) {
  size_t got = 0;
  while (got < len) {
    ssize_t n = read(fd, buf + got, len - got);
    if (n <= 0) return false;
    got += static_cast<size_t>(n);
  }
  return true;
}

void serveClient(int fd) {
  unsigned char tag = 0;
  for (;;) {
    ssize_t n = read(fd, &tag, 1);
    if (n <= 0) break;

    if (tag == 'K') {
      unsigned char rep[8];
      if (!readFully(fd, rep, sizeof(rep))) break;
      std::lock_guard<std::mutex> lock(g_lock);
      writeReport(&g_kbd_fd, kKbdPath, rep, sizeof(rep));
    } else if (tag == 'M') {
      unsigned char rep[4];
      if (!readFully(fd, rep, sizeof(rep))) break;
      std::lock_guard<std::mutex> lock(g_lock);
      writeReport(&g_mouse_fd, kMousePath, rep, sizeof(rep));
    } else if (tag == 'P') {
      if (write(fd, "OK\n", 3) < 0) break;
    } else {
      ALOGW("unknown tag 0x%02x, dropping connection", tag);
      break;
    }
  }
  close(fd);
}

}  // namespace

int main() {
  int srv = socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (srv < 0) { ALOGE("socket: %s", strerror(errno)); return 1; }

  int one = 1;
  setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  addr.sin_port = htons(kPort);
  if (bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
    ALOGE("bind: %s", strerror(errno));
    return 1;
  }
  if (listen(srv, 8) < 0) { ALOGE("listen: %s", strerror(errno)); return 1; }

  ALOGI("hidbridge listening on 127.0.0.1:%u", kPort);

  for (;;) {
    int c = accept4(srv, nullptr, nullptr, SOCK_CLOEXEC);
    if (c < 0) {
      if (errno == EINTR) continue;
      ALOGW("accept: %s", strerror(errno));
      usleep(100000);
      continue;
    }
    std::thread(serveClient, c).detach();
  }
}
