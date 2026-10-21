// Work on a thread of its own, with progress and cancellation, whose result
// is handed back on the window's thread.
#pragma once

#include <functional>
#include <string>

#include <QPointer>
#include <QString>
#include <QThread>

#include "fgw/tools.h"

namespace fgw::app {

class Job : public QThread {
    Q_OBJECT

public:
    // What to do with the result, on the window's thread.
    using Deliver = std::function<void()>;
    // Runs on the job's thread. `progress(fraction, message)` may be called
    // from any thread (fraction 0..1, negative for "busy").
    using Work = std::function<Deliver(CancelToken& cancel, const Progress& progress)>;

    // Starts nothing yet: connect the signals, then start(). A job deletes
    // itself once its outcome is delivered; hold it in a QPointer.
    Job(QString name, Work work, QObject* parent);
    // Cancels the work and waits for it, if it still runs.
    ~Job() override;

    const QString& name() const { return name_; }
    // Asks the work to stop and ends the programs it runs.
    void cancel() { token_.cancel(); }

signals:
    void progressed(double fraction, const QString& message);
    // One of these three follows the end of the work, on the window's
    // thread, after the result was delivered (succeeded), then ended().
    void succeeded();
    void failed(const QString& message);
    void cancelled();
    void ended();

protected:
    void run() override;

private:
    void deliver_outcome();

    enum class Outcome { succeeded, failed, cancelled };

    QString name_;
    Work work_;
    CancelToken token_;
    Outcome outcome_ = Outcome::cancelled;
    Deliver deliver_;
    QString error_;
    bool delivered_ = false;

    friend bool running(const QPointer<Job>& job);
    friend void stop_for_good(const QPointer<Job>& job, unsigned long wait_ms);
};

// Whether `job` is still at work: running, or finished with its outcome not
// yet delivered (a job whose thread has ended reads as running until then,
// so nothing starts in its place before its results are in). A delivered
// job deletes itself and reads as not running.
inline bool running(const QPointer<Job>& job) { return job && !job->delivered_; }

// For a window that is closing: cancels `job`, waits up to `wait_ms` for it
// to end, and drops its outcome, so that nothing it would deliver (results,
// or work started when it ends) arrives any more. A job still running then
// is let go of rather than destroyed with its parent (a running thread
// cannot be): the app's exit ends it, and its programs with it.
void stop_for_good(const QPointer<Job>& job, unsigned long wait_ms);

}  // namespace fgw::app
