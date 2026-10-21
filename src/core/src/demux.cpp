#include "demux.h"

#include <stdexcept>
#include <string>

extern "C" {
#include <libavformat/avformat.h>
}

#include "fgw/text.h"

namespace fgw {

namespace {

std::string av_error_text(int code) {
    char text[AV_ERROR_MAX_STRING_SIZE] = {};
    av_strerror(code, text, sizeof text);
    return text;
}

}  // namespace

Demuxer::Demuxer(const std::filesystem::path& path, int stream_index) : stream_index_(stream_index) {
    if (const int error = avformat_open_input(&format_, utf8(path).c_str(), nullptr, nullptr); error < 0) {
        throw std::runtime_error("cannot read " + utf8(path.filename()) + ": " + av_error_text(error));
    }
    if (const int error = avformat_find_stream_info(format_, nullptr); error < 0) {
        avformat_close_input(&format_);
        throw std::runtime_error("cannot read the streams of " + utf8(path.filename()) + ": " + av_error_text(error));
    }
    if (stream_index_ < 0 || static_cast<unsigned>(stream_index_) >= format_->nb_streams) {
        avformat_close_input(&format_);
        throw std::runtime_error(utf8(path.filename()) + " has no stream " + std::to_string(stream_index));
    }
    packet_ = av_packet_alloc();
}

Demuxer::~Demuxer() {
    av_packet_free(&packet_);
    avformat_close_input(&format_);
}

Rational Demuxer::time_base() const {
    const AVRational base = format_->streams[stream_index_]->time_base;
    return {base.num, base.den};
}

std::span<const std::uint8_t> Demuxer::extradata() const {
    const AVCodecParameters* parameters = format_->streams[stream_index_]->codecpar;
    return {parameters->extradata, static_cast<std::size_t>(parameters->extradata_size)};
}

std::optional<DemuxedPacket> Demuxer::next() {
    while (true) {
        av_packet_unref(packet_);
        if (av_read_frame(format_, packet_) < 0) {
            return std::nullopt;
        }
        if (packet_->stream_index != stream_index_ || packet_->size == 0) {
            continue;
        }
        DemuxedPacket packet;
        packet.data = {packet_->data, static_cast<std::size_t>(packet_->size)};
        if (packet_->pts != AV_NOPTS_VALUE) {
            packet.pts = packet_->pts;
        }
        packet.duration = packet_->duration;
        packet.keyframe = (packet_->flags & AV_PKT_FLAG_KEY) != 0;
        return packet;
    }
}

}  // namespace fgw
