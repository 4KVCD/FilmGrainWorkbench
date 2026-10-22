#include "theme.h"

#include <QApplication>
#include <QColor>
#include <QPalette>

namespace fgw::app {

namespace {

constexpr const char* style_sheet = R"(
QGroupBox { font-weight: 600; border: 1px solid #3a4048; border-radius: 6px; margin-top: 10px; padding: 8px 6px 6px 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9fd3c7; }
QLabel#info { color: #a9b1ba; }
QLabel#warning { color: #1b1f24; background: #e3b341; border-radius: 4px; padding: 6px; }
QPushButton { padding: 5px 10px; }
QPushButton[primary="true"]:enabled { background: #2f8f6f; color: white; font-weight: 600; border: 1px solid #3fae88; border-radius: 4px; }
QPushButton[primary="true"]:enabled:hover { background: #37a57f; }
QPushButton[primary="true"]:disabled { background: #2a2e33; color: #6b737c; border: 1px solid #353a40; border-radius: 4px; }
QPushButton:checked { background: #3d6ea8; color: white; }
)";

QPalette dark_palette() {
    QPalette p;
    const QColor base(30, 33, 37);
    const QColor window(38, 42, 47);
    const QColor text(225, 228, 232);
    const QColor disabled(110, 116, 124);
    const std::pair<QPalette::ColorRole, QColor> roles[] = {
        {QPalette::Window, window},
        {QPalette::WindowText, text},
        {QPalette::Base, base},
        {QPalette::AlternateBase, window},
        {QPalette::ToolTipBase, QColor(50, 55, 61)},
        {QPalette::ToolTipText, text},
        {QPalette::Text, text},
        {QPalette::PlaceholderText, QColor(128, 136, 144)},  // the hint in an empty field: left unset, it is near black
        {QPalette::Button, QColor(48, 53, 59)},
        {QPalette::ButtonText, text},
        {QPalette::Highlight, QColor(61, 110, 168)},
        {QPalette::HighlightedText, QColor(255, 255, 255)},
        {QPalette::Link, QColor(40, 30, 0)},
    };
    for (const auto& [role, color] : roles) {
        p.setColor(role, color);
    }
    for (const QPalette::ColorRole role : {QPalette::WindowText, QPalette::Text, QPalette::ButtonText}) {
        p.setColor(QPalette::Disabled, role, disabled);
    }
    return p;
}

}  // namespace

void apply_theme(QApplication& app) {
    QApplication::setStyle("Fusion");
    QApplication::setPalette(dark_palette());
    app.setStyleSheet(style_sheet);
}

}  // namespace fgw::app
