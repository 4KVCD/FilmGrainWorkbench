#include "job.h"

#include <exception>
#include <utility>

#include <QCoreApplication>

#include "release.h"

namespace fgw::app {

Job::Job(QString name, Work work, QObject* parent) : QThread(parent), name_(std::move(name)), work_(std::move(work)) {
    // QThread::finished comes from the job's thread; this receiver lives on
    // the window's, so the outcome is delivered there.
    connect(this, &QThread::finished, this, &Job::deliver_outcome);
}

Job::~Job() {
    // Destroyed with its parent while it still runs (a job replaced by a
    // newer one, at the app's exit): a running QThread must not be destroyed.
    if (isRunning()) {
        cancel();
        wait();
    }
}

void Job::run() {
    const Progress progress = [this](double fraction, const std::string& message) {
        emit progressed(fraction, QString::fromStdString(message));
    };
    try {
        deliver_ = work_(token_, progress);
        outcome_ = token_.cancelled() ? Outcome::cancelled : Outcome::succeeded;
    } catch (const Cancelled&) {
        outcome_ = Outcome::cancelled;
    } catch (const std::exception& error) {
        outcome_ = token_.cancelled() ? Outcome::cancelled : Outcome::failed;
        error_ = QString::fromStdString(error.what());
    } catch (...) {
        outcome_ = Outcome::failed;
        error_ = QStringLiteral("unexpected error");
    }
    work_ = nullptr;  // what it holds (frames, often) is freed here, not on the window's thread
}

void Job::deliver_outcome() {
    delivered_ = true;  // what is done on delivery (start the next job) sees this one as done
    switch (outcome_) {
        case Outcome::succeeded:
            if (deliver_) {
                deliver_();
            }
            emit succeeded();
            break;
        case Outcome::failed:
            emit failed(error_);
            break;
        case Outcome::cancelled:
            emit cancelled();
            break;
    }
    release_later(std::exchange(deliver_, nullptr));
    emit ended();
    deleteLater();
}

void stop_for_good(const QPointer<Job>& job, unsigned long wait_ms) {
    if (!job || job->delivered_) {
        return;
    }
    Job* const stopped = job.data();
    stopped->cancel();
    stopped->wait(wait_ms);
    stopped->disconnect();                              // no delivery, no ended(): nothing more starts
    QCoreApplication::removePostedEvents(stopped);  // a delivery already queued
    stopped->delivered_ = true;
    if (stopped->isRunning()) {
        stopped->setParent(nullptr);  // left to the app's exit; destroying a running thread aborts
    } else {
        delete stopped;
    }
}

}  // namespace fgw::app
