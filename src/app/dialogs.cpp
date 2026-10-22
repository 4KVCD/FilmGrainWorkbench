#include "dialogs.h"

#include <QApplication>
#include <QClipboard>
#include <QDesktopServices>
#include <QDialogButtonBox>
#include <QFileDialog>
#include <QFileInfo>
#include <QFont>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QPixmap>
#include <QPushButton>
#include <QUrl>
#include <QVBoxLayout>

#include "fgw/installation.h"
#include "fgw/text.h"
#include "job.h"
#include "settings.h"

namespace fgw::app {

namespace {

const std::map<std::string, QString> purposes = {
    {"ffmpeg", "decodes frames"}, {"ffprobe", "reads video properties"}, {"mkvmerge", "writes the output MKV (MKVToolNix)"}};
const std::map<std::string, QString> where_to_get = {
    {"mkvmerge", "Install MKVToolNix from mkvtoolnix.download."},
    {"ffmpeg", "Any recent build with VVC decoding (e.g. gyan.dev full build)."},
    {"ffprobe", "Comes with ffmpeg."}};

// A path as typed or pasted: Explorer's "Copy as path" puts it in quotes.
QString cleaned_path(const QLineEdit* edit) {
    QString path = edit->text().trimmed();
    if (path.size() >= 2 && path.startsWith('"') && path.endsWith('"')) {
        path = path.mid(1, path.size() - 2);
    }
    return path;
}

}  // namespace

ToolsDialog::ToolsDialog(const Tools& tools, QWidget* parent) : QDialog(parent) {
    setWindowTitle("External programs");
    setMinimumWidth(760);
    auto* layout = new QVBoxLayout(this);
    auto* intro = new QLabel("The workbench drives these programs. Paths are found automatically where possible "
                             "and remembered.", this);
    intro->setWordWrap(true);
    layout->addWidget(intro);
    auto* grid = new QGridLayout;
    int row = 0;
    for (const std::string& name : Tools::names()) {
        auto* label = new QLabel("<b>" + QString::fromStdString(Tools::label(name)) + "</b><br><small>" +
                                 purposes.at(name) + "</small>", this);
        auto* edit = new QLineEdit(QString::fromStdString(tools.path(name)), this);
        edit->setToolTip(where_to_get.at(name));
        connect(edit, &QLineEdit::textChanged, this, &ToolsDialog::refresh);
        auto* browse_button = new QPushButton("Browse…", this);
        connect(browse_button, &QPushButton::clicked, this, [this, name] { browse(name); });
        auto* state = new QLabel(this);
        grid->addWidget(label, row, 0);
        grid->addWidget(edit, row, 1);
        grid->addWidget(browse_button, row, 2);
        grid->addWidget(state, row, 3);
        edits_[name] = edit;
        states_[name] = state;
        ++row;
    }
    grid->setColumnStretch(1, 1);
    layout->addLayout(grid);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, this);
    QPushButton* detect = buttons->addButton("Find automatically", QDialogButtonBox::ActionRole);
    connect(detect, &QPushButton::clicked, this, &ToolsDialog::find_automatically);
    connect(buttons, &QDialogButtonBox::accepted, this, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    layout->addWidget(buttons);
    refresh();
}

void ToolsDialog::browse(const std::string& name) {
    const QString path = QFileDialog::getOpenFileName(this, "Locate " + QString::fromStdString(Tools::label(name)),
                                                      edits_.at(name)->text(), "Programs (*.exe);;All files (*)");
    if (!path.isEmpty()) {
        edits_.at(name)->setText(QDir::toNativeSeparators(path));
    }
}

void ToolsDialog::find_automatically() {
    const Tools found = detect_tools();
    for (const auto& [name, edit] : edits_) {
        if (!QFileInfo(cleaned_path(edit)).isFile() && !found.path(name).empty()) {
            edit->setText(QString::fromStdString(found.path(name)));
        }
    }
}

void ToolsDialog::refresh() {
    for (const auto& [name, edit] : edits_) {
        const bool ok = QFileInfo(cleaned_path(edit)).isFile();
        states_.at(name)->setText(ok ? "✔ found" : "✖ missing");
        states_.at(name)->setStyleSheet(ok ? "color: #6fcf8f;" : "color: #ff7b72;");
    }
}

Tools ToolsDialog::tools() const {
    Tools tools;
    for (const auto& [name, edit] : edits_) {
        tools.path(name) = QDir::toNativeSeparators(cleaned_path(edit)).toStdString();
    }
    return tools;
}

AboutDialog::AboutDialog(QWidget* parent) : QDialog(parent) {
    setWindowTitle("About " + app_title);
    auto* layout = new QVBoxLayout(this);
    auto* top = new QHBoxLayout;
    auto* icon = new QLabel(this);
    icon->setPixmap(QPixmap(":/icon.png").scaled(96, 96, Qt::KeepAspectRatio, Qt::SmoothTransformation));
    top->addWidget(icon, 0, Qt::AlignTop);
    auto* text = new QLabel(
        "<h2>" + app_title + " " + QApplication::applicationVersion() +
            "</h2><p>See what VVC's own film grain analysis (VVenC) and synthesis (vvdec) do to a film, scene by "
            "scene; and measure the grain a VVC encode lost, then write it back as AV1 film grain (AFGS1) "
            "metadata.</p><p>Bundled: VVenC and vvdec (Fraunhofer HHI, BSD 3-Clause Clear), a grain analyzer "
            "built on libaom's noise model (AOMedia, BSD 2-Clause), Qt (LGPL v3) and FFmpeg's libraries "
            "(LGPL 2.1). Their licenses are in the <i>licenses</i> folder.</p>",
        this);
    text->setWordWrap(true);
    text->setTextInteractionFlags(Qt::TextBrowserInteraction);
    text->setMinimumWidth(460);
    top->addWidget(text, 1);
    layout->addLayout(top);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Close, this);
    const QString licenses = QString::fromStdString(utf8(app_dir() / "licenses"));
    if (QFileInfo(licenses).isDir()) {
        QPushButton* show = buttons->addButton("Open licenses folder", QDialogButtonBox::ActionRole);
        connect(show, &QPushButton::clicked, this, [licenses] { QDesktopServices::openUrl(QUrl::fromLocalFile(licenses)); });
    }
    connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    layout->addWidget(buttons);
}

InstallationDialog::InstallationDialog(const Tools& tools, const VvcTools& vvc_tools, QWidget* parent)
    : QDialog(parent), text_(new QPlainTextEdit(this)) {
    setWindowTitle("Check installation");
    resize(820, 460);
    auto* layout = new QVBoxLayout(this);
    text_->setReadOnly(true);
    text_->setLineWrapMode(QPlainTextEdit::NoWrap);
    QFont mono("Consolas");
    mono.setStyleHint(QFont::Monospace);
    text_->setFont(mono);
    text_->setPlainText("Asking each program for its version…");
    layout->addWidget(text_);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Close, this);
    QPushButton* copy = buttons->addButton("Copy", QDialogButtonBox::ActionRole);
    connect(copy, &QPushButton::clicked, this, [this] { QApplication::clipboard()->setText(text_->toPlainText()); });
    connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    layout->addWidget(buttons);

    // Not the dialog's child: closed before the checks end, the dialog must
    // not take a running thread with it. The job deletes itself when done,
    // and shows the report only if the dialog is still there.
    const std::string title = (app_title + " " + QApplication::applicationVersion()).toStdString();
    const QPointer<QPlainTextEdit> shown = text_;
    auto* job = new Job("Installation check", [shown, tools, vvc_tools, title](CancelToken&, const Progress&) -> Job::Deliver {
        const std::string report = format_installation_report(check_installation(tools, vvc_tools), title);
        return [shown, report] {
            if (shown) {
                shown->setPlainText(QString::fromStdString(report));
            }
        };
    }, nullptr);
    job->start();
}

}  // namespace fgw::app
