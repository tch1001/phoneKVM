/*
 * uac2sink — make the phone behave as a USB speaker.
 *
 * The UAC2 gadget function (host -> device, i.e. "capture" from the gadget's
 * point of view) exposes an ALSA capture PCM. This daemon reads that PCM and
 * writes it to Android's primary output through AAudio, so audio played on
 * the USB host comes out of the phone's speaker.
 *
 * Lifecycle: started once by init and left running. It waits for the gadget
 * PCM to appear, bridges while the cable is in, and goes back to waiting when
 * the gadget disappears (unplug / rebind), so replugging never restarts it.
 */

#define LOG_TAG "uac2sink"

#include <aaudio/AAudio.h>
#include <log/log.h>
#include <tinyalsa/asoundlib.h>

#include <fcntl.h>
#include <sound/asound.h>
#include <sys/ioctl.h>

#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <unistd.h>

namespace {

constexpr int kRate = 48000;
constexpr int kChannels = 2;
constexpr int kPeriodFrames = 1024;
constexpr int kPeriodCount = 4;

// Find the gadget card by asking ALSA directly.
//
// Neither lookup-by-name file works for a platform-domain daemon here:
// /proc/asound/cards is labeled vendor_proc_audiod (a vendor type the platform
// policy cannot name at all) and coredomain.te forbids coredomains from
// reading generic sysfs. The control device is labeled audio_device, which we
// already use for the PCM, and SNDRV_CTL_IOCTL_CARD_INFO reports the card id.
int findUac2Card() {
  for (int idx = 0; idx < 32; ++idx) {
    char path[64];
    snprintf(path, sizeof(path), "/dev/snd/controlC%d", idx);

    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) continue;

    struct snd_ctl_card_info info;
    memset(&info, 0, sizeof(info));
    int rc = ioctl(fd, SNDRV_CTL_IOCTL_CARD_INFO, &info);
    close(fd);
    if (rc < 0) continue;

    if (strstr(reinterpret_cast<const char*>(info.id), "UAC2") != nullptr ||
        strstr(reinterpret_cast<const char*>(info.name), "UAC2") != nullptr) {
      return idx;
    }
  }
  return -1;
}

// One live bridge: gadget capture PCM -> AAudio output. Returns when the
// gadget goes away or the output stream fails.
void bridgeOnce(int card) {
  struct pcm_config cfg;
  memset(&cfg, 0, sizeof(cfg));
  cfg.channels = kChannels;
  cfg.rate = kRate;
  cfg.period_size = kPeriodFrames;
  cfg.period_count = kPeriodCount;
  cfg.format = PCM_FORMAT_S16_LE;
  cfg.start_threshold = kPeriodFrames;
  cfg.stop_threshold = 0;
  cfg.silence_threshold = 0;

  struct pcm* in = pcm_open(card, 0, PCM_IN, &cfg);
  if (in == nullptr || !pcm_is_ready(in)) {
    ALOGW("cannot open capture PCM on card %d: %s", card,
          in ? pcm_get_error(in) : "null");
    if (in) pcm_close(in);
    return;
  }

  AAudioStreamBuilder* builder = nullptr;
  if (AAudio_createStreamBuilder(&builder) != AAUDIO_OK || builder == nullptr) {
    ALOGE("AAudio builder failed");
    pcm_close(in);
    return;
  }

  AAudioStreamBuilder_setDirection(builder, AAUDIO_DIRECTION_OUTPUT);
  AAudioStreamBuilder_setSampleRate(builder, kRate);
  AAudioStreamBuilder_setChannelCount(builder, kChannels);
  AAudioStreamBuilder_setFormat(builder, AAUDIO_FORMAT_PCM_I16);
  AAudioStreamBuilder_setPerformanceMode(builder, AAUDIO_PERFORMANCE_MODE_LOW_LATENCY);
  AAudioStreamBuilder_setUsage(builder, AAUDIO_USAGE_MEDIA);
  AAudioStreamBuilder_setContentType(builder, AAUDIO_CONTENT_TYPE_MUSIC);

  AAudioStream* out = nullptr;
  aaudio_result_t res = AAudioStreamBuilder_openStream(builder, &out);
  AAudioStreamBuilder_delete(builder);
  if (res != AAUDIO_OK || out == nullptr) {
    ALOGE("cannot open AAudio output: %s", AAudio_convertResultToText(res));
    pcm_close(in);
    return;
  }

  if (AAudioStream_requestStart(out) != AAUDIO_OK) {
    ALOGE("cannot start AAudio stream");
    AAudioStream_close(out);
    pcm_close(in);
    return;
  }

  ALOGI("bridging UAC2 card %d -> speaker (%d Hz, %d ch)", card, kRate, kChannels);

  const size_t frames = kPeriodFrames;
  int16_t* buf = static_cast<int16_t*>(malloc(frames * kChannels * sizeof(int16_t)));
  if (buf == nullptr) {
    AAudioStream_close(out);
    pcm_close(in);
    return;
  }

  while (true) {
    int rc = pcm_read(in, buf, frames * kChannels * sizeof(int16_t));
    if (rc != 0) {
      // Unplug / rebind: the capture node goes away.
      ALOGI("capture stopped (%s), waiting for the gadget again",
            pcm_get_error(in));
      break;
    }

    int64_t off = 0;
    bool wrote_error = false;
    while (off < static_cast<int64_t>(frames)) {
      int32_t n = AAudioStream_write(out, buf + off * kChannels, frames - off, 200000000LL);
      if (n < 0) {
        ALOGE("AAudio write failed: %s", AAudio_convertResultToText(n));
        wrote_error = true;
        break;
      }
      if (n == 0) continue;  // timeout, retry
      off += n;
    }
    if (wrote_error) break;
  }

  free(buf);
  AAudioStream_requestStop(out);
  AAudioStream_close(out);
  pcm_close(in);
}

}  // namespace

int main(int argc, char** argv) {
  ALOGI("uac2sink started; waiting for the UAC2 gadget");

  // Stay resident for the life of the system: plug/unplug only takes the PCM
  // away, it must not require restarting this process.
  while (true) {
    int card = findUac2Card();
    if (card < 0) {
      usleep(500 * 1000);
      continue;
    }

    bridgeOnce(card);

    // Let a rebind settle before looking for the card again.
    usleep(500 * 1000);
  }

  return 0;
}
