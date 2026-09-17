"""Large-document plain-text editor support for raw KML."""

from __future__ import annotations

import re

from PyQt6.QtCore import QEvent, QRect, QSize, Qt
from PyQt6.QtGui import (
    QColor,
    QFontDatabase,
    QFontInfo,
    QPainter,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PyQt6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from services import KmlSourceLocation


class KmlSyntaxHighlighter(QSyntaxHighlighter):
    """Incremental XML highlighting with bounded work for giant coordinate lines."""

    MAX_HIGHLIGHT_CHARS = 32_768
    _TAG_RE = re.compile(r"</?\s*([A-Za-z_][\w:.-]*)")
    _ATTRIBUTE_RE = re.compile(r"\s([A-Za-z_][\w:.-]*)(?=\s*=)")
    _STRING_RE = re.compile(r"(['\"])(.*?)\1")

    def __init__(self, document, palette: QPalette):
        super().__init__(document)
        self._last_scan_length = 0
        self.refresh_formats(palette)

    @staticmethod
    def _format(colour: QColor, *, italic: bool = False, bold: bool = False):
        value = QTextCharFormat()
        value.setForeground(colour)
        value.setFontItalic(italic)
        value.setFontWeight(700 if bold else 400)
        return value

    def refresh_formats(self, palette: QPalette) -> None:
        link = palette.color(QPalette.ColorRole.Link)
        text = palette.color(QPalette.ColorRole.Text)
        accent = palette.color(QPalette.ColorRole.Highlight)
        muted = palette.color(QPalette.ColorRole.PlaceholderText)
        self.tag_format = self._format(link, bold=True)
        self.attribute_format = self._format(accent)
        self.string_format = self._format(text.darker(125) if text.lightness() > 128 else text.lighter(135))
        self.comment_format = self._format(muted, italic=True)
        self.declaration_format = self._format(link, italic=True)
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt override
        scan_length = min(len(text), self.MAX_HIGHLIGHT_CHARS)
        self._last_scan_length = scan_length
        sample = text[:scan_length]

        if sample.lstrip().startswith("<?") or sample.lstrip().startswith("<!DOCTYPE"):
            self.setFormat(0, scan_length, self.declaration_format)

        for match in self._TAG_RE.finditer(sample):
            self.setFormat(match.start(1), len(match.group(1)), self.tag_format)
        for match in self._ATTRIBUTE_RE.finditer(sample):
            self.setFormat(match.start(1), len(match.group(1)), self.attribute_format)
        for match in self._STRING_RE.finditer(sample):
            self.setFormat(match.start(), len(match.group()), self.string_format)

        comment_start = 0 if self.previousBlockState() == 1 else sample.find("<!--")
        while comment_start >= 0 and comment_start < scan_length:
            comment_end = sample.find("-->", comment_start)
            if comment_end < 0:
                self.setFormat(comment_start, scan_length - comment_start, self.comment_format)
                self.setCurrentBlockState(1)
                break
            length = comment_end + 3 - comment_start
            self.setFormat(comment_start, length, self.comment_format)
            comment_start = sample.find("<!--", comment_end + 3)


class _LineNumberArea(QWidget):
    def __init__(self, editor: "KmlCodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor
        self.setAccessibleName("KML editor line numbers")

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.editor.paint_line_numbers(event)


class KmlCodeEditor(QPlainTextEdit):
    """A no-wrap code editor whose gutter work scales with visible blocks."""

    GUTTER_LEFT_PADDING = 7
    GUTTER_RIGHT_PADDING = 8
    GUTTER_SEPARATOR_WIDTH = 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.line_number_area = _LineNumberArea(self)
        self.highlighter = KmlSyntaxHighlighter(self.document(), self.palette())
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_gutter_width()
        self._highlight_current_line()

    def line_number_area_width(self) -> int:
        widest_label = str(max(99, self.blockCount()))
        return (
            self.GUTTER_LEFT_PADDING
            + self.fontMetrics().horizontalAdvance(widest_label)
            + self.GUTTER_RIGHT_PADDING
            + self.GUTTER_SEPARATOR_WIDTH
        )

    def _update_gutter_width(self, _count: int = 0) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)
        self._update_gutter_geometry()

    def _update_gutter_geometry(self) -> None:
        contents = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(contents.left(), contents.top(), self.line_number_area_width(), contents.height())
        )

    def _update_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_gutter_geometry()

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), self.palette().color(QPalette.ColorRole.AlternateBase))
        painter.setFont(self.font())
        separator_x = self.line_number_area.width() - self.GUTTER_SEPARATOR_WIDTH
        painter.setPen(self.palette().color(QPalette.ColorRole.Mid))
        painter.drawLine(separator_x, event.rect().top(), separator_x, event.rect().bottom())
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        colour = self.palette().color(QPalette.ColorRole.PlaceholderText)
        painter.setPen(colour)
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    self.GUTTER_LEFT_PADDING,
                    top,
                    max(
                        1,
                        self.line_number_area.width()
                        - self.GUTTER_LEFT_PADDING
                        - self.GUTTER_RIGHT_PADDING
                        - self.GUTTER_SEPARATOR_WIDTH,
                    ),
                    max(1, bottom - top),
                    int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                    str(number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    def _highlight_current_line(self) -> None:
        selection = QTextEdit.ExtraSelection()
        colour = self.palette().color(QPalette.ColorRole.AlternateBase)
        colour.setAlpha(95)
        selection.format.setBackground(colour)
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selection.cursor = self.textCursor()
        selection.cursor.clearSelection()
        self.setExtraSelections([selection])

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() in {
            QEvent.Type.PaletteChange,
            QEvent.Type.FontChange,
        } and hasattr(self, "highlighter"):
            self.highlighter.refresh_formats(self.palette())
            self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
            self._update_gutter_width()
            self.line_number_area.update()

    @staticmethod
    def default_font_point_size() -> int:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        size = QFontInfo(font).pointSize()
        return size if size > 0 else 12

    def set_font_point_size(self, point_size: int) -> None:
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        font.setPointSize(int(point_size))
        self.setFont(font)

    def go_to_location(self, location: KmlSourceLocation) -> bool:
        cursor = self.textCursor()
        plain_text = self.toPlainText()
        if location.offset is not None and 0 <= location.offset <= len(plain_text):
            selected_end = min(len(plain_text), location.offset + location.length)
            start = len(plain_text[: location.offset].encode("utf-16-le")) // 2
            end = len(plain_text[:selected_end].encode("utf-16-le")) // 2
        else:
            block = self.document().findBlockByNumber(max(0, location.line - 1))
            if not block.isValid():
                return False
            start = min(block.position() + max(0, location.column - 1), block.position() + block.length() - 1)
            end = start
        cursor.setPosition(start)
        if end > start:
            cursor.setPosition(
                min(end, self.document().characterCount() - 1),
                QTextCursor.MoveMode.KeepAnchor,
            )
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        return True


__all__ = ["KmlCodeEditor", "KmlSyntaxHighlighter"]
