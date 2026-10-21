// Freeing big things away from the window's thread.
#pragma once

#include <utility>

#include <QThreadPool>

namespace fgw::app {

// Destroys `value` on a pool thread. Freeing decoded 4K frames and pictures
// (tens of MB each) takes milliseconds the window's thread should not spend:
// dropping a pair's cached source frames took 9 ms there.
template <typename T>
void release_later(T value) {
    QThreadPool::globalInstance()->start([value = std::move(value)]() mutable { T gone = std::move(value); });
}

}  // namespace fgw::app
