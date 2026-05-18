import asyncio
import base64
import json
import logging
import mimetypes
import os
import time
import queue
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, LEFT, RIGHT, PhotoImage, StringVar, Tk, filedialog, messagebox, ttk
from tkinter import scrolledtext
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin
import webbrowser

from bs4 import BeautifulSoup
from playwright.async_api import Browser, BrowserContext, Error, Page, Playwright, async_playwright


APP_NAME = "Web Capture Automation Studio"
APP_EXECUTABLE_NAME = "WebCaptureAutomationStudio"
APP_VERSION = "0.9.0"
APP_STATE_DIR = Path.cwd() / ".save-page-desktop"
SETTINGS_PATH = APP_STATE_DIR / "settings.json"
DEFAULT_PROJECT_PATH = APP_STATE_DIR / "last-project.json"
LOG_PATH = APP_STATE_DIR / "app.log"
ICON_PATH = Path.cwd() / "assets" / "web-capture-automation-studio.ico"
DEFAULT_WINDOW_GEOMETRY = "1640x980"


SNAPSHOT_JS = r"""
() => {
    const clone = document.documentElement.cloneNode(true);

    const liveInputs = Array.from(document.querySelectorAll('input'));
    const cloneInputs = Array.from(clone.querySelectorAll('input'));
    liveInputs.forEach((live, index) => {
        const copy = cloneInputs[index];
        if (!copy) return;
        const type = (live.getAttribute('type') || '').toLowerCase();
        if (type === 'checkbox' || type === 'radio') {
            if (live.checked) copy.setAttribute('checked', 'checked');
            else copy.removeAttribute('checked');
        } else {
            copy.setAttribute('value', live.value ?? '');
        }
    });

    const liveTextareas = Array.from(document.querySelectorAll('textarea'));
    const cloneTextareas = Array.from(clone.querySelectorAll('textarea'));
    liveTextareas.forEach((live, index) => {
        const copy = cloneTextareas[index];
        if (copy) copy.textContent = live.value ?? '';
    });

    const liveSelects = Array.from(document.querySelectorAll('select'));
    const cloneSelects = Array.from(clone.querySelectorAll('select'));
    liveSelects.forEach((live, index) => {
        const copy = cloneSelects[index];
        if (!copy) return;
        Array.from(copy.options).forEach((option, optionIndex) => {
            option.selected = live.options[optionIndex]?.selected || false;
            if (option.selected) option.setAttribute('selected', 'selected');
            else option.removeAttribute('selected');
        });
    });

    const liveCanvases = Array.from(document.querySelectorAll('canvas'));
    const cloneCanvases = Array.from(clone.querySelectorAll('canvas'));
    liveCanvases.forEach((live, index) => {
        const copy = cloneCanvases[index];
        if (!copy) return;
        try {
            const img = document.createElement('img');
            img.setAttribute('src', live.toDataURL('image/png'));
            img.setAttribute('data-original-tag', 'canvas');
            img.setAttribute('width', String(live.width));
            img.setAttribute('height', String(live.height));
            copy.replaceWith(img);
        } catch (error) {
            copy.setAttribute('data-save-warning', 'canvas-export-failed');
        }
    });

    return {
        url: location.href,
        title: document.title || '',
        doctype: document.doctype
            ? `<!DOCTYPE ${document.doctype.name}>`
            : '<!DOCTYPE html>',
        html: clone.outerHTML
    };
}
"""

INSPECT_SELECTOR_JS = r"""
selector => {
    const elements = Array.from(document.querySelectorAll(selector));
    const first = elements[0];
    if (!first) {
        return { found: false, count: 0 };
    }

    const getXPath = (el) => {
        if (el.id) return `//*[@id="${el.id}"]`;
        const parts = [];
        while (el && el.nodeType === Node.ELEMENT_NODE) {
            let index = 1;
            let sibling = el.previousElementSibling;
            while (sibling) {
                if (sibling.tagName === el.tagName) index += 1;
                sibling = sibling.previousElementSibling;
            }
            parts.unshift(`${el.tagName.toLowerCase()}[${index}]`);
            el = el.parentElement;
        }
        return '/' + parts.join('/');
    };

    const attrs = {};
    for (const attr of first.attributes) {
        attrs[attr.name] = attr.value;
    }

    const rect = first.getBoundingClientRect();
    const text = (first.innerText || first.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 150);
    const tag = first.tagName.toLowerCase();
    const id = first.id || '';
    const name = first.getAttribute('name') || '';
    const dataTestId = first.getAttribute('data-testid') || first.getAttribute('data-test') || '';
    const ariaLabel = first.getAttribute('aria-label') || '';
    const placeholder = first.getAttribute('placeholder') || '';
    const type = first.getAttribute('type') || '';
    const classes = Array.from(first.classList).slice(0, 4);

    return {
        found: true,
        count: elements.length,
        tag,
        id,
        name,
        dataTestId,
        ariaLabel,
        placeholder,
        type,
        text,
        classes,
        attrs,
        xpath: getXPath(first),
        outerHtml: first.outerHTML.slice(0, 1500),
        rect: {
            x: Math.round(rect.x),
            y: Math.round(rect.y),
            width: Math.round(rect.width),
            height: Math.round(rect.height)
        }
    };
}
"""

HIGHLIGHT_SELECTOR_JS = r"""
options => {
    const selector = options.selector;
    const palette = options.palette || ['#ff4d4f', '#1677ff', '#52c41a', '#faad14', '#722ed1'];
    const elements = Array.from(document.querySelectorAll(selector));
    elements.forEach((element, index) => {
        element.scrollIntoView({ block: 'center', inline: 'center' });
        element.style.outline = `3px solid ${palette[index % palette.length]}`;
        element.style.outlineOffset = '2px';
        element.setAttribute('data-savepage-highlight', String(index));
        setTimeout(() => {
            element.style.outline = '';
            element.style.outlineOffset = '';
            element.removeAttribute('data-savepage-highlight');
        }, 2500);
    });
    return elements.length;
}
"""

INSPECTOR_BOOTSTRAP_JS = r"""
() => {
    if (window.__savePageDesktopInspectorInstalled) {
        window.__savePageDesktopInspector?.ensurePanel();
        return;
    }
    window.__savePageDesktopInspectorInstalled = true;

    const palette = ['#ff4d4f', '#1677ff', '#52c41a', '#faad14', '#722ed1'];
    let hoverTarget = null;
    let active = false;
    let overlay = null;
    let button = null;
    let panel = null;
    let statusLabel = null;
    let isDragging = false;
    let dragOffsetX = 0;
    let dragOffsetY = 0;
    let panelWidth = 260;
    let panelHeight = 170;
    let isMaximized = false;
    let previousBounds = null;

    const getXPath = (el) => {
        if (el.id) return `//*[@id="${el.id}"]`;
        const parts = [];
        while (el && el.nodeType === Node.ELEMENT_NODE) {
            let index = 1;
            let sibling = el.previousElementSibling;
            while (sibling) {
                if (sibling.tagName === el.tagName) index += 1;
                sibling = sibling.previousElementSibling;
            }
            parts.unshift(`${el.tagName.toLowerCase()}[${index}]`);
            el = el.parentElement;
        }
        return '/' + parts.join('/');
    };

    const buildDetails = (el) => {
        const attrs = {};
        for (const attr of el.attributes) attrs[attr.name] = attr.value;
        const rect = el.getBoundingClientRect();
        return {
            found: true,
            count: 1,
            tag: el.tagName.toLowerCase(),
            id: el.id || '',
            name: el.getAttribute('name') || '',
            dataTestId: el.getAttribute('data-testid') || el.getAttribute('data-test') || '',
            ariaLabel: el.getAttribute('aria-label') || '',
            placeholder: el.getAttribute('placeholder') || '',
            type: el.getAttribute('type') || '',
            text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 150),
            classes: Array.from(el.classList).slice(0, 4),
            attrs,
            xpath: getXPath(el),
            outerHtml: el.outerHTML.slice(0, 1500),
            rect: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height)
            }
        };
    };

    const ensureOverlay = () => {
        if (overlay && document.body.contains(overlay)) return overlay;
        overlay = document.createElement('div');
        overlay.id = '__save_page_desktop_overlay__';
        overlay.style.position = 'fixed';
        overlay.style.pointerEvents = 'none';
        overlay.style.zIndex = '2147483646';
        overlay.style.border = '3px solid #1677ff';
        overlay.style.background = 'rgba(22,119,255,0.10)';
        overlay.style.borderRadius = '4px';
        overlay.style.display = 'none';
        document.body.appendChild(overlay);
        return overlay;
    };

    const paintHover = (element, colorIndex = 1) => {
        const box = ensureOverlay();
        const rect = element.getBoundingClientRect();
        box.style.left = `${rect.left}px`;
        box.style.top = `${rect.top}px`;
        box.style.width = `${rect.width}px`;
        box.style.height = `${rect.height}px`;
        box.style.borderColor = palette[colorIndex % palette.length];
        box.style.background = `${palette[colorIndex % palette.length]}22`;
        box.style.display = 'block';
    };

    const clearHover = () => {
        if (overlay) overlay.style.display = 'none';
    };

    const stop = () => {
        active = false;
        hoverTarget = null;
        clearHover();
        if (button) button.style.background = '#111827';
        if (statusLabel) statusLabel.textContent = 'Idle';
        document.removeEventListener('mouseover', onMouseOver, true);
        document.removeEventListener('click', onClick, true);
        document.removeEventListener('keydown', onKeyDown, true);
    };

    const start = () => {
        active = true;
        if (button) button.style.background = '#1677ff';
        if (statusLabel) statusLabel.textContent = 'Capturing click...';
        document.addEventListener('mouseover', onMouseOver, true);
        document.addEventListener('click', onClick, true);
        document.addEventListener('keydown', onKeyDown, true);
    };

    const toggle = () => {
        if (active) stop();
        else start();
    };

    const onMouseOver = (event) => {
        if (!active) return;
        const target = event.target;
        if (!target || target === button || button?.contains(target)) return;
        hoverTarget = target;
        paintHover(target, 1);
    };

    const onClick = async (event) => {
        if (!active) return;
        const target = event.target;
        if (!target || target === button || button?.contains(target)) return;
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        hoverTarget = target;
        paintHover(target, 2);
        const details = buildDetails(target);
        try {
            await window.savePageDesktopInspect(details);
        } catch (error) {
            console.error(error);
        }
        stop();
    };

    const onKeyDown = (event) => {
        if (event.key === 'Escape') stop();
    };

    const ensureButton = () => {
        if (button && document.body.contains(button)) return;
        button = document.createElement('button');
        button.type = 'button';
        button.textContent = 'Start Capture';
        button.style.padding = '8px 12px';
        button.style.border = '0';
        button.style.borderRadius = '8px';
        button.style.background = '#111827';
        button.style.color = '#ffffff';
        button.style.font = '600 13px Segoe UI, sans-serif';
        button.style.cursor = 'pointer';
        button.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            start();
        });
    };

    const clampPanelToViewport = () => {
        if (!panel) return;
        const maxLeft = Math.max(0, window.innerWidth - panel.offsetWidth - 8);
        const maxTop = Math.max(0, window.innerHeight - panel.offsetHeight - 8);
        const currentLeft = parseInt(panel.style.left || "0", 10);
        const currentTop = parseInt(panel.style.top || "0", 10);
        panel.style.left = `${Math.min(Math.max(8, currentLeft), maxLeft)}px`;
        panel.style.top = `${Math.min(Math.max(8, currentTop), maxTop)}px`;
    };

    const applyPanelSize = () => {
        if (!panel) return;
        panel.style.width = `${panelWidth}px`;
        panel.style.height = `${panelHeight}px`;
        clampPanelToViewport();
    };

    const maximizePanel = () => {
        if (!panel || isMaximized) return;
        previousBounds = {
            left: panel.style.left,
            top: panel.style.top,
            width: panelWidth,
            height: panelHeight
        };
        isMaximized = true;
        panel.style.left = '12px';
        panel.style.top = '12px';
        panelWidth = Math.max(320, window.innerWidth - 24);
        panelHeight = Math.max(220, window.innerHeight - 24);
        applyPanelSize();
    };

    const restorePanel = () => {
        if (!panel || !isMaximized) return;
        isMaximized = false;
        panel.style.left = previousBounds?.left || '16px';
        panel.style.top = previousBounds?.top || '16px';
        panelWidth = previousBounds?.width || 260;
        panelHeight = previousBounds?.height || 170;
        applyPanelSize();
    };

    const toggleMaximize = () => {
        if (isMaximized) restorePanel();
        else maximizePanel();
    };

    const ensurePanel = () => {
        if (panel && document.body.contains(panel)) return;
        ensureButton();
        panel = document.createElement('div');
        panel.id = '__save_page_desktop_panel__';
        panel.style.position = 'fixed';
        panel.style.left = `${Math.max(16, window.innerWidth - panelWidth - 16)}px`;
        panel.style.top = `${Math.max(16, window.innerHeight - panelHeight - 16)}px`;
        panel.style.zIndex = '2147483647';
        panel.style.width = `${panelWidth}px`;
        panel.style.height = `${panelHeight}px`;
        panel.style.minWidth = '220px';
        panel.style.minHeight = '140px';
        panel.style.resize = 'both';
        panel.style.overflow = 'auto';
        panel.style.padding = '12px';
        panel.style.borderRadius = '14px';
        panel.style.background = 'rgba(17, 24, 39, 0.96)';
        panel.style.color = '#ffffff';
        panel.style.boxShadow = '0 18px 38px rgba(0,0,0,0.30)';
        panel.style.font = '13px Segoe UI, sans-serif';
        panel.style.display = 'flex';
        panel.style.flexDirection = 'column';
        panel.style.gap = '10px';

        const titleBar = document.createElement('div');
        titleBar.style.display = 'flex';
        titleBar.style.alignItems = 'center';
        titleBar.style.justifyContent = 'space-between';
        titleBar.style.gap = '8px';
        titleBar.style.cursor = 'move';
        titleBar.style.userSelect = 'none';

        const title = document.createElement('div');
        title.textContent = 'Web Capture Automation Studio Capture';
        title.style.fontWeight = '700';

        const windowActions = document.createElement('div');
        windowActions.style.display = 'flex';
        windowActions.style.gap = '6px';

        const maximizeButton = document.createElement('button');
        maximizeButton.type = 'button';
        maximizeButton.textContent = '[]';
        maximizeButton.title = 'Maximize / Restore';
        maximizeButton.style.padding = '4px 8px';
        maximizeButton.style.border = '0';
        maximizeButton.style.borderRadius = '6px';
        maximizeButton.style.background = '#374151';
        maximizeButton.style.color = '#ffffff';
        maximizeButton.style.cursor = 'pointer';
        maximizeButton.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            toggleMaximize();
        });

        statusLabel = document.createElement('div');
        statusLabel.textContent = 'Idle';
        statusLabel.style.fontSize = '12px';
        statusLabel.style.color = '#bfdbfe';

        const controls = document.createElement('div');
        controls.style.display = 'flex';
        controls.style.gap = '8px';

        const stopButton = document.createElement('button');
        stopButton.type = 'button';
        stopButton.textContent = 'Stop';
        stopButton.style.padding = '8px 12px';
        stopButton.style.border = '0';
        stopButton.style.borderRadius = '8px';
        stopButton.style.background = '#4b5563';
        stopButton.style.color = '#ffffff';
        stopButton.style.cursor = 'pointer';
        stopButton.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            stop();
        });

        const note = document.createElement('div');
        note.textContent = 'Click Start Capture, then click any element on the page.';
        note.style.fontSize = '12px';
        note.style.lineHeight = '1.35';
        note.style.color = '#d1d5db';

        titleBar.addEventListener('pointerdown', (event) => {
            if (event.target === maximizeButton) return;
            isDragging = true;
            dragOffsetX = event.clientX - panel.getBoundingClientRect().left;
            dragOffsetY = event.clientY - panel.getBoundingClientRect().top;
            titleBar.setPointerCapture?.(event.pointerId);
        });
        titleBar.addEventListener('pointermove', (event) => {
            if (!isDragging || isMaximized) return;
            panel.style.left = `${event.clientX - dragOffsetX}px`;
            panel.style.top = `${event.clientY - dragOffsetY}px`;
            clampPanelToViewport();
        });
        titleBar.addEventListener('pointerup', () => {
            isDragging = false;
        });
        titleBar.addEventListener('pointercancel', () => {
            isDragging = false;
        });

        new ResizeObserver(() => {
            if (!panel || isMaximized) return;
            panelWidth = panel.offsetWidth;
            panelHeight = panel.offsetHeight;
            clampPanelToViewport();
        }).observe(panel);

        window.addEventListener('resize', () => {
            if (isMaximized) {
                panelWidth = Math.max(320, window.innerWidth - 24);
                panelHeight = Math.max(220, window.innerHeight - 24);
                applyPanelSize();
            } else {
                clampPanelToViewport();
            }
        });

        windowActions.appendChild(maximizeButton);
        titleBar.appendChild(title);
        titleBar.appendChild(windowActions);
        controls.appendChild(button);
        controls.appendChild(stopButton);
        panel.appendChild(titleBar);
        panel.appendChild(statusLabel);
        panel.appendChild(controls);
        panel.appendChild(note);
        document.body.appendChild(panel);
    };

    window.__savePageDesktopInspector = {
        ensurePanel,
        activateFromHost: start,
        deactivate: stop
    };

    ensurePanel();
}
"""

PAGE_SUMMARY_JS = r"""
() => ({
    title: document.title || '',
    url: location.href,
    readyState: document.readyState,
    forms: document.forms.length,
    links: document.links.length,
    images: document.images.length,
    buttons: document.querySelectorAll('button,input[type="button"],input[type="submit"]').length,
    inputs: document.querySelectorAll('input,textarea,select').length,
    iframes: document.querySelectorAll('iframe').length
})
"""

LOCATOR_CATALOG_JS = r"""
() => {
    const nodes = Array.from(document.querySelectorAll('input, textarea, select, button, a'));
    return nodes.slice(0, 200).map((node, index) => {
        const text = (node.innerText || node.textContent || node.value || '').trim().replace(/\s+/g, ' ').slice(0, 80);
        return {
            index: index + 1,
            tag: node.tagName.toLowerCase(),
            id: node.id || '',
            name: node.getAttribute('name') || '',
            dataTestId: node.getAttribute('data-testid') || node.getAttribute('data-test') || '',
            ariaLabel: node.getAttribute('aria-label') || '',
            type: node.getAttribute('type') || '',
            text
        };
    });
}
"""

URL_PATTERN = re.compile(r"url\((.*?)\)", re.IGNORECASE)
SRCSET_SPLIT = re.compile(r"\s*,\s*")


@dataclass
class CachedResource:
    url: str
    body: bytes
    content_type: str


def sanitize_filename(value: str, fallback: str = "saved-page", max_length: int = 80) -> str:
    safe = re.sub(r'[<>:"/\\|?*]+', "_", value or "").strip(" .")
    safe = re.sub(r"\s+", " ", safe)
    return (safe[:max_length].strip() or fallback)


def java_string_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def java_field_name(seed: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", seed or "")
    if not words:
        return "pageElement"
    first = words[0].lower()
    rest = "".join(word.capitalize() for word in words[1:])
    candidate = f"{first}{rest}"
    if candidate[0].isdigit():
        candidate = f"element{candidate}"
    return candidate[:60]


def java_class_name(seed: str, fallback: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", seed or "")
    if not words:
        return fallback
    candidate = "".join(word.capitalize() for word in words)
    if candidate[0].isdigit():
        candidate = f"Generated{candidate}"
    return candidate


def human_label(details: Dict[str, Any]) -> str:
    return (
        details.get("ariaLabel")
        or details.get("name")
        or details.get("id")
        or details.get("placeholder")
        or details.get("text")
        or details.get("tag")
        or "element"
    )


def readable_field_name(field_name: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", field_name).replace("_", " ").lower()


def infer_generator_action(element: Dict[str, Any]) -> str:
    tag = (element.get("tag") or "").lower()
    type_value = (element.get("type") or "").lower()
    attrs = element.get("attrs") or {}
    classes = " ".join(element.get("classes") or []).lower()
    role = str(attrs.get("role", "")).lower()
    aria_modal = str(attrs.get("aria-modal", "")).lower()
    if tag == "table":
        return "table"
    if tag == "dialog" or role == "dialog" or aria_modal == "true" or "modal" in classes or "dialog" in classes:
        return "modal"
    if role in {"alert", "status"} or any(token in classes for token in ("toast", "snackbar", "notification", "alert")):
        return "toast"
    if tag == "select":
        return "select"
    if tag in {"textarea"}:
        return "type"
    if tag == "input":
        if type_value == "file":
            return "file"
        if type_value == "date":
            return "date"
        if type_value in {"checkbox"}:
            return "checkbox"
        if type_value in {"radio"}:
            return "radio"
        if type_value in {"submit", "button", "reset", "image"}:
            return "click"
        return "type"
    return "click"


def build_locator_candidates(details: Dict[str, Any]) -> List[Dict[str, str]]:
    locators: List[Dict[str, str]] = []
    if details.get("dataTestId"):
        locators.append({"type": "css", "value": f'[data-testid="{details["dataTestId"]}"]', "reason": "stable test id"})
    if details.get("id"):
        locators.append({"type": "id", "value": details["id"], "reason": "direct id"})
    if details.get("name"):
        locators.append({"type": "name", "value": details["name"], "reason": "form control name"})
    if details.get("ariaLabel") and details.get("tag"):
        locators.append({
            "type": "css",
            "value": f'{details["tag"]}[aria-label="{details["ariaLabel"]}"]',
            "reason": "accessible label",
        })
    if details.get("placeholder") and details.get("tag"):
        locators.append({
            "type": "css",
            "value": f'{details["tag"]}[placeholder="{details["placeholder"]}"]',
            "reason": "placeholder",
        })
    if details.get("tag") and details.get("classes"):
        class_selector = ".".join(re.sub(r"[^A-Za-z0-9_-]", "", cls) for cls in details["classes"] if cls)
        if class_selector:
            locators.append({"type": "css", "value": f'{details["tag"]}.{class_selector}', "reason": "tag plus classes"})
    if details.get("text"):
        text = details["text"][:60]
        locators.append({
            "type": "xpath",
            "value": f'//{details["tag"]}[contains(normalize-space(.), "{text}")]',
            "reason": "visible text",
        })
    if details.get("xpath"):
        locators.append({"type": "xpath", "value": details["xpath"], "reason": "absolute fallback"})
    return locators


def build_page_factory_snippet(details: Dict[str, Any]) -> str:
    locators = details.get("locators", [])
    preferred = locators[0] if locators else {"type": "xpath", "value": details.get("xpath", "//body")}
    field_name = java_field_name(details.get("ariaLabel") or details.get("name") or details.get("id") or details.get("text") or details.get("tag"))
    value = java_string_literal(preferred["value"])
    annotation_map = {
        "id": f'@FindBy(id = "{value}")',
        "name": f'@FindBy(name = "{value}")',
        "css": f'@FindBy(css = "{value}")',
        "xpath": f'@FindBy(xpath = "{value}")',
    }
    annotation = annotation_map.get(preferred["type"], f'@FindBy(xpath = "{value}")')
    by_map = {
        "id": f'By.id("{value}")',
        "name": f'By.name("{value}")',
        "css": f'By.cssSelector("{value}")',
        "xpath": f'By.xpath("{value}")',
    }
    by_expr = by_map.get(preferred["type"], f'By.xpath("{value}")')
    return "\n".join(
        [
            annotation,
            f"private WebElement {field_name};",
            "",
            f"private final By {field_name}By = {by_expr};",
            "",
            f"public void click{field_name[0].upper() + field_name[1:]}() {{",
            f"    wait.until(ExpectedConditions.elementToBeClickable({field_name}By)).click();",
            "}",
        ]
    )


def enrich_element_details(details: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(details)
    enriched["locators"] = build_locator_candidates(enriched)
    enriched["page_factory"] = build_page_factory_snippet(enriched)
    return enriched


def preferred_locator(element: Dict[str, Any]) -> Dict[str, str]:
    locators = element.get("locators") or []
    return locators[0] if locators else {"type": "xpath", "value": element.get("xpath", "//body")}


def normalize_step_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def build_runner_element_map(elements: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    for element in elements:
        readable = readable_field_name(element["field_name"])
        mapping[normalize_step_key(readable)] = element
    return mapping


def parse_generated_gherkin(feature_text: str, elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    element_map = build_runner_element_map(elements)
    parsed_steps: List[Dict[str, Any]] = []
    previous_keyword = ""
    patterns = [
        ("open_url", re.compile(r'^Given I open "(.*)"$')),
        ("clear_cookies", re.compile(r"^Given I clear browser cookies$")),
        ("clear_storage", re.compile(r"^Given I clear browser storage$")),
        ("restore_cookies", re.compile(r'^Given I restore cookies from "(.*)"$')),
        ("restore_storage", re.compile(r'^Given I restore storage from "(.*)"$')),
        ("set_browser_size", re.compile(r'^Given I set browser size to "(.*)"$')),
        ("switch_iframe", re.compile(r'^Given I switch to iframe "(.*)"$')),
        ("switch_main_content", re.compile(r"^Given I switch to main content$")),
        ("switch_newest_window", re.compile(r"^Given I switch to the newest window$")),
        ("switch_window_title", re.compile(r'^Given I switch to window with title "(.*)"$')),
        ("enter_value", re.compile(r'^When I enter "(.*)" into the (.+) field$')),
        ("assert_value", re.compile(r'^Then the (.+) field value should be "(.*)"$')),
        ("upload_file", re.compile(r'^When I upload file "(.*)" into the (.+) field$')),
        ("assert_value_contains", re.compile(r'^Then the (.+) field value should contain "(.*)"$')),
        ("enter_date", re.compile(r'^When I enter date "(.*)" into the (.+) field$')),
        ("select_option", re.compile(r'^When I select "(.*)" from the (.+) dropdown$')),
        ("assert_selected_option", re.compile(r'^Then the selected (.+) option should be "(.*)"$')),
        ("set_checkbox", re.compile(r'^When I set the (.+) checkbox to "(.*)"$')),
        ("assert_checkbox_selected", re.compile(r'^Then the (.+) checkbox should be selected$')),
        ("assert_checkbox_not_selected", re.compile(r'^Then the (.+) checkbox should not be selected$')),
        ("select_radio", re.compile(r'^When I select the (.+) radio option$')),
        ("assert_radio_selected", re.compile(r'^Then the (.+) radio option should be selected$')),
        ("click_element", re.compile(r'^When I click the (.+) element$')),
        ("hover_element", re.compile(r'^When I hover over the (.+) element$')),
        ("press_key_global", re.compile(r'^When I press key "(.*)"$')),
        ("press_key_on_element", re.compile(r'^When I press key "(.*)" on the (.+) element$')),
        ("drag_to_element", re.compile(r'^When I drag the (.+) element to the (.+) element$')),
        ("assert_visible", re.compile(r'^Then the (.+) element should be visible$')),
        ("assert_text", re.compile(r'^Then the (.+) element text should be "(.*)"$')),
        ("assert_text_contains", re.compile(r'^Then the (.+) element text should contain "(.*)"$')),
        ("assert_modal_visible", re.compile(r'^Then the (.+) modal should be visible$')),
        ("assert_modal_text_contains", re.compile(r'^Then the (.+) modal text should contain "(.*)"$')),
        ("assert_toast_visible", re.compile(r'^Then the (.+) toast should be visible$')),
        ("assert_toast_text_contains", re.compile(r'^Then the (.+) toast text should contain "(.*)"$')),
        ("assert_table_row_contains", re.compile(r'^Then the (.+) table should contain row text "(.*)"$')),
        ("wait_element_disappear", re.compile(r'^Then I wait for the (.+) element to disappear$')),
        ("wait_text_appear", re.compile(r'^Then I wait for text "(.*)" to appear in the (.+) element$')),
        ("wait_url_contains", re.compile(r'^Then I wait for URL to contain "(.*)"$')),
        ("wait_page_idle", re.compile(r"^Then I wait for the page to become idle$")),
        ("wait_milliseconds", re.compile(r"^Then I wait for (\d+) milliseconds$")),
        ("assert_page_title", re.compile(r'^Then the page title should be "(.*)"$')),
    ]

    for raw_line in feature_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("Feature:", "Scenario:", "#")):
            continue
        if line.startswith(("Given ", "When ", "Then ", "And ", "But ")):
            if line.startswith("And "):
                if previous_keyword:
                    line = previous_keyword + " " + line[4:]
                else:
                    line = "When " + line[4:]
            elif line.startswith("But "):
                if previous_keyword:
                    line = previous_keyword + " " + line[4:]
                else:
                    line = "Then " + line[4:]
            previous_keyword = line.split(" ", 1)[0]
        else:
            continue

        matched = False
        for step_type, pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue
            matched = True
            if step_type in {
                "clear_cookies",
                "clear_storage",
                "switch_main_content",
                "switch_newest_window",
                "wait_page_idle",
            }:
                parsed_steps.append({"type": step_type, "raw": raw_line})
                break
            if step_type in {"open_url", "restore_cookies", "restore_storage", "set_browser_size", "switch_iframe", "switch_window_title", "press_key_global", "wait_url_contains", "assert_page_title"}:
                parsed_steps.append({"type": step_type, "value": match.group(1), "raw": raw_line})
                if step_type == "assert_page_title":
                    parsed_steps[-1]["expected"] = match.group(1)
                    parsed_steps[-1].pop("value", None)
                break
            if step_type == "wait_milliseconds":
                parsed_steps.append({"type": step_type, "value": int(match.group(1)), "raw": raw_line})
                break

            if step_type in {"enter_value", "upload_file", "enter_date", "select_option", "set_checkbox"}:
                value, readable = match.group(1), match.group(2)
            elif step_type in {"press_key_on_element", "wait_text_appear"}:
                value, readable = match.group(1), match.group(2)
            elif step_type == "drag_to_element":
                source_readable, target_readable = match.group(1), match.group(2)
                source_element = element_map.get(normalize_step_key(source_readable))
                target_element = element_map.get(normalize_step_key(target_readable))
                if not source_element:
                    raise RuntimeError(f'No queued locator matches drag source "{source_readable}" in line: {raw_line.strip()}')
                if not target_element:
                    raise RuntimeError(f'No queued locator matches drag target "{target_readable}" in line: {raw_line.strip()}')
                parsed_steps.append(
                    {
                        "type": step_type,
                        "source_element": source_element,
                        "target_element": target_element,
                        "raw": raw_line,
                    }
                )
                break
            elif step_type in {
                "assert_value",
                "assert_value_contains",
                "assert_selected_option",
                "assert_text",
                "assert_text_contains",
                "assert_modal_text_contains",
                "assert_toast_text_contains",
                "assert_table_row_contains",
            }:
                readable, value = match.group(1), match.group(2)
            else:
                readable = match.group(1)
                value = None

            element = element_map.get(normalize_step_key(readable))
            if not element:
                raise RuntimeError(f'No queued locator matches Gherkin reference "{readable}" in line: {raw_line.strip()}')
            parsed_steps.append(
                {
                    "type": step_type,
                    "value": value,
                    "element": element,
                    "raw": raw_line,
                }
            )
            break
        if not matched and line.startswith(("Given ", "When ", "Then ")):
            raise RuntimeError(f"Unsupported step format: {raw_line.strip()}")
    return parsed_steps


def open_path_in_shell(path: Path) -> None:
    target = path.resolve()
    if not target.exists():
        raise RuntimeError(f"Path does not exist: {target}")
    if hasattr(os, "startfile"):
        os.startfile(str(target))
        return
    webbrowser.open(target.as_uri())


def build_app_logo() -> PhotoImage:
    image = PhotoImage(width=64, height=64)
    image.put("#0f2747", to=(0, 0, 64, 64))
    image.put("#ffffff", to=(12, 8, 48, 56))
    image.put("#d7e7ff", to=(39, 8, 48, 17))
    image.put("#aac7f5", to=(39, 17, 48, 26))
    image.put("#1e5aa8", to=(18, 26, 43, 49))
    image.put("#8fc4ff", to=(22, 30, 39, 36))
    image.put("#dcecff", to=(22, 39, 39, 44))
    image.put("#0f2747", to=(16, 22, 44, 24))
    image.put("#0f2747", to=(16, 50, 44, 52))
    image.put("#0f2747", to=(16, 22, 18, 52))
    image.put("#0f2747", to=(44, 22, 46, 52))
    image.put("#1cb36b", to=(41, 41, 58, 58))
    image.put("#ffffff", to=(45, 48, 49, 52))
    image.put("#ffffff", to=(49, 44, 53, 48))
    image.put("#ffffff", to=(53, 40, 57, 44))
    return image


class BrowserWorker:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.resource_cache: Dict[str, CachedResource] = {}
        self.pending_browser_pick: Optional[Dict[str, Any]] = None
        self.current_frame_selector: Optional[str] = None

    async def _launch_browser_instance(self) -> Browser:
        attempts: List[tuple[str, Dict[str, Any]]] = []
        if os.name == "nt":
            attempts.extend(
                [
                    ("Microsoft Edge", {"channel": "msedge", "headless": False}),
                    ("Google Chrome", {"channel": "chrome", "headless": False}),
                ]
            )
        attempts.append(("Playwright Chromium", {"headless": False}))

        failures: List[str] = []
        for label, options in attempts:
            try:
                browser = await self.playwright.chromium.launch(**options)
                logging.info("Launched browser using %s.", label)
                return browser
            except Exception as exc:
                failures.append(f"{label}: {exc}")
                logging.warning("Browser launch attempt failed for %s: %s", label, exc)

        raise RuntimeError(
            "Unable to launch a supported browser. Install Microsoft Edge or Google Chrome, "
            "or run `playwright install chromium` for the unpackaged Python version.\n\n"
            + "\n".join(failures)
        )

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _ensure_page(self) -> Page:
        if not self.page or self.page.is_closed():
            await self.launch_browser()
        assert self.page is not None
        return self.page

    async def launch_browser(self, initial_url: Optional[str] = None) -> None:
        if self.page and not self.page.is_closed():
            await self.page.bring_to_front()
            if initial_url:
                await self.navigate(initial_url)
            return

        self.playwright = await async_playwright().start()
        self.resource_cache.clear()
        self.browser = await self._launch_browser_instance()
        self.context = await self.browser.new_context(viewport={"width": 1366, "height": 900})
        await self.context.expose_binding("savePageDesktopInspect", self._handle_browser_pick)
        await self.context.add_init_script(INSPECTOR_BOOTSTRAP_JS)
        self.context.on("page", self._bind_page_events)
        page = await self.context.new_page()
        self._bind_page_events(page)
        target_url = initial_url.strip() if initial_url else "https://example.com"
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target_url):
            target_url = f"https://{target_url}"
        self.current_frame_selector = None
        await page.goto(target_url, wait_until="domcontentloaded")
        await self._ensure_page_inspector(page)

    def _bind_page_events(self, page: Page) -> None:
        self.page = page
        page.on("response", lambda response: self.run(self._cache_response(response)))
        page.on("load", lambda: self.run(self._ensure_page_inspector(page)))

    async def _ensure_page_inspector(self, page: Page) -> None:
        try:
            await page.evaluate(INSPECTOR_BOOTSTRAP_JS)
        except Error:
            return

    async def _handle_browser_pick(self, _source, details: Dict[str, Any]) -> None:
        self.pending_browser_pick = enrich_element_details(details)

    async def get_pending_browser_pick(self) -> Optional[Dict[str, Any]]:
        picked = self.pending_browser_pick
        self.pending_browser_pick = None
        return picked

    async def _cache_response(self, response) -> None:
        try:
            body = await response.body()
        except Error:
            return
        content_type = response.headers.get("content-type", "application/octet-stream")
        self.resource_cache[response.url] = CachedResource(response.url, body, content_type)

    async def navigate(self, url: str) -> None:
        page = await self._ensure_page()
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", url):
            url = f"https://{url}"
        self.current_frame_selector = None
        await page.goto(url, wait_until="domcontentloaded")

    async def current_info(self) -> Dict[str, str]:
        if not self.page or self.page.is_closed():
            return {"url": "", "title": "", "status": "Browser not running"}
        return {"url": self.page.url, "title": await self.page.title(), "status": "Ready"}

    async def page_summary(self) -> Dict[str, Any]:
        page = await self._ensure_page()
        return await page.evaluate(PAGE_SUMMARY_JS)

    async def locator_catalog(self) -> List[Dict[str, Any]]:
        page = await self._ensure_page()
        return await page.evaluate(LOCATOR_CATALOG_JS)

    async def _fetch_resource(self, url: str) -> Optional[CachedResource]:
        cached = self.resource_cache.get(url)
        if cached:
            return cached

        if not self.context or not self.page or self.page.is_closed():
            return None

        try:
            response = await self.context.request.get(url, fail_on_status_code=False, timeout=15000)
        except Error:
            return None

        if not response.ok and response.status >= 400:
            return None

        try:
            body = await response.body()
        except Error:
            return None

        content_type = response.headers.get("content-type", mimetypes.guess_type(url)[0] or "application/octet-stream")
        resource = CachedResource(url, body, content_type)
        self.resource_cache[url] = resource
        return resource

    async def save_current_page(self, destination: Path) -> str:
        page = await self._ensure_page()
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Error:
            pass
        snapshot = await page.evaluate(SNAPSHOT_JS)
        html = await self._build_single_file_html(snapshot["url"], snapshot["doctype"], snapshot["html"])
        destination.write_text(html, encoding="utf-8")
        return snapshot["title"] or destination.stem

    async def save_viewport_screenshot(self, destination: Path) -> None:
        page = await self._ensure_page()
        await page.screenshot(path=str(destination), full_page=False)

    async def save_full_page_screenshot(self, destination: Path) -> None:
        page = await self._ensure_page()
        await page.screenshot(path=str(destination), full_page=True)

    async def wait_for_selector(self, selector: str, timeout_ms: int) -> Dict[str, Any]:
        page = await self._ensure_page()
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout_ms)
        return await self.inspect_selector(selector)

    async def inspect_selector(self, selector: str) -> Dict[str, Any]:
        page = await self._ensure_page()
        result = await page.evaluate(INSPECT_SELECTOR_JS, selector)
        if not result.get("found"):
            raise RuntimeError(f"No element matched selector: {selector}")
        await page.evaluate(HIGHLIGHT_SELECTOR_JS, {"selector": selector})
        return enrich_element_details(result)

    async def highlight_selector(self, selector: str) -> int:
        page = await self._ensure_page()
        return await page.evaluate(HIGHLIGHT_SELECTOR_JS, {"selector": selector})

    async def enable_browser_picker(self) -> None:
        page = await self._ensure_page()
        await self._ensure_page_inspector(page)
        await page.evaluate("() => window.__savePageDesktopInspector?.activateFromHost()")

    async def run_gherkin_steps(self, steps: List[Dict[str, Any]], timeout_ms: int, step_delay_ms: int) -> Dict[str, Any]:
        page = await self._ensure_page()
        logs: List[str] = []
        artifact_dir = Path.cwd() / "runner-artifacts" / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self.current_frame_selector = None
        try:
            for index, step in enumerate(steps, start=1):
                await self._execute_gherkin_step(page, step, timeout_ms)
                logs.append(f"{index:02d}. PASS - {step['raw'].strip()}")
                if step_delay_ms > 0:
                    await page.wait_for_timeout(step_delay_ms)
        except Exception as exc:
            screenshot_path = artifact_dir / "failure.png"
            html_path = artifact_dir / "failure.html"
            log_path = artifact_dir / "execution.log"
            try:
                await page.screenshot(path=str(screenshot_path), full_page=True)
            except Exception:
                pass
            try:
                html_path.write_text(await page.content(), encoding="utf-8")
            except Exception:
                pass
            log_path.write_text("\n".join(logs + [f"FAIL - {exc}"]), encoding="utf-8")
            return {"success": False, "logs": logs, "error": str(exc), "artifact_dir": str(artifact_dir)}

        report_path = artifact_dir / "execution.log"
        report_path.write_text("\n".join(logs), encoding="utf-8")
        return {"success": True, "logs": logs, "artifact_dir": str(artifact_dir)}

    async def _execute_gherkin_step(self, page: Page, step: Dict[str, Any], timeout_ms: int) -> None:
        step_type = step["type"]
        if step_type == "open_url":
            await self.navigate(step["value"])
            return
        if step_type == "clear_cookies":
            if not self.context:
                raise RuntimeError("Browser context is not available.")
            await self.context.clear_cookies()
            return
        if step_type == "clear_storage":
            await page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
            return
        if step_type == "restore_cookies":
            cookie_path = Path(step["value"])
            if not cookie_path.exists():
                raise RuntimeError(f"Cookie file not found: {cookie_path}")
            if not self.context:
                raise RuntimeError("Browser context is not available.")
            cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
            await self.context.add_cookies(cookies)
            return
        if step_type == "restore_storage":
            storage_path = Path(step["value"])
            if not storage_path.exists():
                raise RuntimeError(f"Storage file not found: {storage_path}")
            storage = json.loads(storage_path.read_text(encoding="utf-8"))
            await page.evaluate(
                """(payload) => {
                    localStorage.clear();
                    sessionStorage.clear();
                    for (const [key, value] of Object.entries(payload.localStorage || {})) localStorage.setItem(key, value);
                    for (const [key, value] of Object.entries(payload.sessionStorage || {})) sessionStorage.setItem(key, value);
                }""",
                arg=storage,
            )
            return
        if step_type == "set_browser_size":
            match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", str(step["value"]))
            if not match:
                raise RuntimeError(f'Browser size must look like "1366x900", got: {step["value"]}')
            await page.set_viewport_size({"width": int(match.group(1)), "height": int(match.group(2))})
            return
        if step_type == "switch_iframe":
            self.current_frame_selector = step["value"]
            frame_locator = page.frame_locator(step["value"]).locator("body")
            await frame_locator.wait_for(state="attached", timeout=timeout_ms)
            return
        if step_type == "switch_main_content":
            self.current_frame_selector = None
            return
        if step_type == "switch_newest_window":
            if not self.context or not self.context.pages:
                raise RuntimeError("No browser windows are open.")
            self.page = self.context.pages[-1]
            self.current_frame_selector = None
            await self.page.bring_to_front()
            return
        if step_type == "switch_window_title":
            if not self.context:
                raise RuntimeError("Browser context is not available.")
            for candidate in self.context.pages:
                if await candidate.title() == step["value"]:
                    self.page = candidate
                    self.current_frame_selector = None
                    await candidate.bring_to_front()
                    return
            raise RuntimeError(f'No window with title "{step["value"]}" was found.')
        if step_type == "assert_page_title":
            await self._wait_until(lambda: page.title(), lambda actual: actual == step["expected"], timeout_ms, f'Page title did not become "{step["expected"]}"')
            return
        if step_type == "press_key_global":
            await page.keyboard.press(step["value"])
            return
        if step_type == "wait_url_contains":
            await self._wait_until(lambda: self._get_page_url(page), lambda actual: step["value"] in actual, timeout_ms, f'URL did not contain "{step["value"]}"')
            return
        if step_type == "wait_page_idle":
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
            return
        if step_type == "wait_milliseconds":
            await page.wait_for_timeout(step["value"])
            return

        element = step["element"]
        locator_info = preferred_locator(element)
        locator = self._locator_from_preferred(page, locator_info)
        await locator.first.scroll_into_view_if_needed(timeout=timeout_ms)

        if step_type == "enter_value":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            await locator.first.fill(step["value"], timeout=timeout_ms)
            return
        if step_type == "upload_file":
            file_path = Path(step["value"])
            if not file_path.exists():
                raise RuntimeError(f'File not found for upload step: {file_path}')
            await locator.first.wait_for(state="attached", timeout=timeout_ms)
            await locator.first.set_input_files(str(file_path), timeout=timeout_ms)
            return
        if step_type == "enter_date":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            await locator.first.fill(step["value"], timeout=timeout_ms)
            return
        if step_type == "assert_value":
            await self._wait_until(lambda: locator.first.input_value(timeout=timeout_ms), lambda actual: actual == step["value"], timeout_ms, f'Field value did not become "{step["value"]}"')
            return
        if step_type == "assert_value_contains":
            await self._wait_until(lambda: locator.first.input_value(timeout=timeout_ms), lambda actual: step["value"] in actual, timeout_ms, f'Field value did not contain "{step["value"]}"')
            return
        if step_type == "select_option":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            await locator.first.select_option(label=step["value"], timeout=timeout_ms)
            return
        if step_type == "assert_selected_option":
            await self._wait_until(
                lambda: locator.first.evaluate("(el) => el.selectedOptions && el.selectedOptions.length ? el.selectedOptions[0].textContent.trim() : ''", timeout=timeout_ms),
                lambda actual: actual == step["value"],
                timeout_ms,
                f'Selected option did not become "{step["value"]}"',
            )
            return
        if step_type == "set_checkbox":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            should_be_checked = str(step["value"]).strip().lower() in {"checked", "true", "yes"}
            await locator.first.set_checked(should_be_checked, timeout=timeout_ms)
            return
        if step_type == "assert_checkbox_selected":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            if not await locator.first.is_checked(timeout=timeout_ms):
                raise RuntimeError(f'Checkbox was not selected for step: {step["raw"].strip()}')
            return
        if step_type == "assert_checkbox_not_selected":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            if await locator.first.is_checked(timeout=timeout_ms):
                raise RuntimeError(f'Checkbox was unexpectedly selected for step: {step["raw"].strip()}')
            return
        if step_type == "select_radio":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            await locator.first.check(timeout=timeout_ms)
            return
        if step_type == "assert_radio_selected":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            if not await locator.first.is_checked(timeout=timeout_ms):
                raise RuntimeError(f'Radio option was not selected for step: {step["raw"].strip()}')
            return
        if step_type == "click_element":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            await locator.first.click(timeout=timeout_ms)
            return
        if step_type == "hover_element":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            await locator.first.hover(timeout=timeout_ms)
            return
        if step_type == "press_key_on_element":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            await locator.first.press(step["value"], timeout=timeout_ms)
            return
        if step_type == "drag_to_element":
            source_locator = self._locator_from_preferred(page, preferred_locator(step["source_element"]))
            target_locator = self._locator_from_preferred(page, preferred_locator(step["target_element"]))
            await source_locator.first.wait_for(state="visible", timeout=timeout_ms)
            await target_locator.first.wait_for(state="visible", timeout=timeout_ms)
            await source_locator.first.drag_to(target_locator.first, timeout=timeout_ms)
            return
        if step_type == "assert_visible":
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            return
        if step_type == "assert_text":
            await self._wait_until(lambda: self._locator_text(locator, timeout_ms), lambda actual: actual == step["value"], timeout_ms, f'Element text did not become "{step["value"]}"')
            return
        if step_type in {"assert_text_contains", "assert_modal_text_contains", "assert_toast_text_contains"}:
            await self._wait_until(lambda: self._locator_text(locator, timeout_ms), lambda actual: step["value"] in actual, timeout_ms, f'Element text did not contain "{step["value"]}"')
            return
        if step_type in {"assert_modal_visible", "assert_toast_visible"}:
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            return
        if step_type == "assert_table_row_contains":
            await self._wait_until(lambda: self._locator_text(locator, timeout_ms), lambda actual: step["value"] in actual, timeout_ms, f'Table did not contain row text "{step["value"]}"')
            return
        if step_type == "wait_element_disappear":
            await locator.first.wait_for(state="hidden", timeout=timeout_ms)
            return
        if step_type == "wait_text_appear":
            await self._wait_until(lambda: self._locator_text(locator, timeout_ms), lambda actual: step["value"] in actual, timeout_ms, f'Text "{step["value"]}" did not appear')
            return
        raise RuntimeError(f"Unsupported runtime step type: {step_type}")

    def _locator_from_preferred(self, page: Page, locator_info: Dict[str, str]):
        container = page if not self.current_frame_selector else page.frame_locator(self.current_frame_selector)
        locator_type = locator_info["type"]
        locator_value = locator_info["value"]
        if locator_type == "id":
            return container.locator(f'xpath=//*[@id="{locator_value}"]')
        if locator_type == "name":
            return container.locator(f'xpath=//*[@name="{locator_value}"]')
        if locator_type == "xpath":
            return container.locator(f"xpath={locator_value}")
        return container.locator(locator_value)

    async def _wait_until(self, producer, predicate, timeout_ms: int, message: str) -> None:
        deadline = time.monotonic() + (timeout_ms / 1000)
        last_value = None
        while time.monotonic() < deadline:
            try:
                last_value = await producer()
                if predicate(last_value):
                    return
            except Exception:
                pass
            await asyncio.sleep(0.2)
        raise RuntimeError(f"{message}. Last value: {last_value}")

    async def _locator_text(self, locator, timeout_ms: int) -> str:
        await locator.first.wait_for(state="visible", timeout=timeout_ms)
        return ((await locator.first.text_content(timeout=timeout_ms)) or "").strip()

    async def _get_page_url(self, page: Page) -> str:
        return page.url

    async def run_javascript(self, script: str) -> str:
        page = await self._ensure_page()
        result = await page.evaluate(f"() => {{ {script} }}")
        return json.dumps(result, indent=2, ensure_ascii=False, default=str)

    async def export_cookies(self, destination: Path) -> int:
        if not self.context:
            raise RuntimeError("Browser is not running.")
        cookies = await self.context.cookies()
        destination.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
        return len(cookies)

    async def export_storage(self, destination: Path) -> Dict[str, int]:
        page = await self._ensure_page()
        storage = await page.evaluate(
            """() => ({
                localStorage: Object.fromEntries(Object.entries(localStorage)),
                sessionStorage: Object.fromEntries(Object.entries(sessionStorage))
            })"""
        )
        destination.write_text(json.dumps(storage, indent=2), encoding="utf-8")
        return {
            "localStorage": len(storage.get("localStorage", {})),
            "sessionStorage": len(storage.get("sessionStorage", {})),
        }

    async def batch_save_pages(self, urls: List[str], destination_dir: Path) -> List[str]:
        page = await self._ensure_page()
        destination_dir.mkdir(parents=True, exist_ok=True)
        saved_files: List[str] = []
        for index, raw_url in enumerate(urls, start=1):
            url = raw_url.strip()
            if not url:
                continue
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", url):
                url = f"https://{url}"
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Error:
                pass
            title = await page.title()
            name = sanitize_filename(f"{index:02d}_{title or 'page'}")
            target = destination_dir / f"{name}.html"
            await self.save_current_page(target)
            saved_files.append(str(target))
        return saved_files

    async def _build_single_file_html(self, page_url: str, doctype: str, html_text: str) -> str:
        soup = BeautifulSoup(html_text, "html.parser")
        if not soup.head:
            head = soup.new_tag("head")
            if soup.html:
                soup.html.insert(0, head)
        if soup.head:
            meta_url = soup.new_tag("meta")
            meta_url["name"] = "save-page-desktop-original-url"
            meta_url["content"] = page_url
            soup.head.append(meta_url)
            meta_saved = soup.new_tag("meta")
            meta_saved["name"] = "save-page-desktop-saved-at"
            meta_saved["content"] = datetime.now().isoformat()
            soup.head.append(meta_saved)
        for tag in soup.find_all("base"):
            tag.decompose()
        await self._inline_stylesheets(soup, page_url)
        await self._inline_scripts(soup, page_url)
        await self._inline_element_urls(soup, page_url)
        await self._inline_style_attributes(soup, page_url)
        self._remove_meta_csp(soup)
        return f"{doctype}\n{str(soup)}"

    async def _inline_stylesheets(self, soup: BeautifulSoup, page_url: str) -> None:
        for link in list(soup.find_all("link")):
            rel_values = [value.lower() for value in (link.get("rel") or [])]
            href = link.get("href")
            if not href:
                continue
            absolute = urljoin(page_url, href)
            if "stylesheet" in rel_values:
                resource = await self._fetch_resource(absolute)
                if not resource:
                    continue
                css_text = resource.body.decode("utf-8", errors="replace")
                css_text = await self._rewrite_css_urls(css_text, absolute)
                style_tag = soup.new_tag("style")
                style_tag["data-save-source"] = absolute
                style_tag.string = css_text
                link.replace_with(style_tag)
            elif any(rel in rel_values for rel in ("icon", "shortcut", "apple-touch-icon", "mask-icon")):
                data_url = await self._resource_to_data_url(absolute)
                if data_url:
                    link["href"] = data_url

        for style_tag in soup.find_all("style"):
            if style_tag.string:
                style_tag.string = await self._rewrite_css_urls(style_tag.string, page_url)

    async def _inline_scripts(self, soup: BeautifulSoup, page_url: str) -> None:
        for script in soup.find_all("script"):
            src = script.get("src")
            if not src:
                continue
            absolute = urljoin(page_url, src)
            resource = await self._fetch_resource(absolute)
            if not resource:
                continue
            script.string = resource.body.decode("utf-8", errors="replace")
            del script["src"]

    async def _inline_element_urls(self, soup: BeautifulSoup, page_url: str) -> None:
        attr_names = ["src", "href", "poster"]
        allowed_tags = {"img", "audio", "video", "source", "track", "embed", "iframe"}
        for tag in soup.find_all(True):
            if tag.name in allowed_tags:
                for attr in attr_names:
                    value = tag.get(attr)
                    if not value:
                        continue
                    if attr == "href" and tag.name != "a":
                        data_url = await self._resource_to_data_url(urljoin(page_url, value))
                        if data_url:
                            tag[attr] = data_url
                    elif attr != "href":
                        data_url = await self._resource_to_data_url(urljoin(page_url, value))
                        if data_url:
                            tag[attr] = data_url
            if tag.name in {"img", "source"} and tag.get("srcset"):
                tag["srcset"] = await self._rewrite_srcset(tag["srcset"], page_url)

    async def _inline_style_attributes(self, soup: BeautifulSoup, page_url: str) -> None:
        for tag in soup.find_all(style=True):
            tag["style"] = await self._rewrite_css_urls(tag["style"], page_url)

    def _remove_meta_csp(self, soup: BeautifulSoup) -> None:
        for meta in soup.find_all("meta"):
            equiv = (meta.get("http-equiv") or "").lower()
            if equiv == "content-security-policy":
                meta.decompose()

    async def _rewrite_css_urls(self, css_text: str, base_url: str) -> str:
        matches = list(URL_PATTERN.finditer(css_text))
        if not matches:
            return css_text
        rebuilt: List[str] = []
        last_index = 0
        for match in matches:
            rebuilt.append(css_text[last_index:match.start()])
            raw = match.group(1).strip().strip("\"'")
            if raw.startswith(("data:", "blob:", "#")):
                rebuilt.append(match.group(0))
            else:
                absolute = urljoin(base_url, raw)
                data_url = await self._resource_to_data_url(absolute)
                rebuilt.append(f"url('{data_url}')" if data_url else match.group(0))
            last_index = match.end()
        rebuilt.append(css_text[last_index:])
        return "".join(rebuilt)

    async def _rewrite_srcset(self, srcset: str, page_url: str) -> str:
        rewritten = []
        for candidate in SRCSET_SPLIT.split(srcset.strip()):
            if not candidate:
                continue
            parts = candidate.split()
            absolute = urljoin(page_url, parts[0])
            data_url = await self._resource_to_data_url(absolute)
            parts[0] = data_url or parts[0]
            rewritten.append(" ".join(parts))
        return ", ".join(rewritten)

    async def _resource_to_data_url(self, absolute_url: str) -> Optional[str]:
        resource = await self._fetch_resource(absolute_url)
        if not resource:
            return None
        mime = resource.content_type.split(";")[0].strip() or "application/octet-stream"
        payload = base64.b64encode(resource.body).decode("ascii")
        return f"data:{mime};base64,{payload}"

    async def shutdown(self) -> None:
        if self.context:
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None


class SavePageApp:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1080x760")
        self.logo_image = build_app_logo()
        self.root.iconphoto(True, self.logo_image)
        if ICON_PATH.exists():
            try:
                self.root.iconbitmap(default=str(ICON_PATH))
            except Exception:
                logging.exception("Failed to load ICO application icon.")
        self.worker = BrowserWorker()
        self.messages: "queue.Queue[tuple[str, str]]" = queue.Queue()

        self.url_var = StringVar(value="https://example.com")
        self.title_var = StringVar(value="Title: ")
        self.status_var = StringVar(value="Status: Browser not running")
        self.current_url_var = StringVar(value="Current URL: ")
        self.selector_var = StringVar(value="button")
        self.wait_timeout_var = StringVar(value="10000")
        self.batch_status_var = StringVar(value="Batch: idle")
        self.page_class_var = StringVar(value="LoginPage")
        self.steps_class_var = StringVar(value="LoginSteps")
        self.step_def_class_var = StringVar(value="LoginStepDefinitions")
        self.queue_filter_var = StringVar(value="")
        self.output_dir_var = StringVar(value="")
        self.package_var = StringVar(value="com.example.automation")
        self.generator_page_url_var = StringVar(value="")
        self.step_target_var = StringVar(value="")
        self.step_template_var = StringVar(value="Click Element")
        self.step_value_var = StringVar(value="")
        self.runner_timeout_var = StringVar(value="10000")
        self.runner_step_delay_var = StringVar(value="150")
        self.runner_status_var = StringVar(value="Runner: idle")
        self.inspected_element: Optional[Dict[str, Any]] = None
        self.pending_capture_element: Optional[Dict[str, Any]] = None
        self.generator_elements: List[Dict[str, Any]] = []
        self.custom_gherkin_steps: List[str] = []
        self.is_dirty = False
        self.last_output_dir: Optional[Path] = None
        self.last_runner_artifact_dir: Optional[Path] = None
        self.last_project_path: Optional[Path] = None

        self.inspect_output: scrolledtext.ScrolledText
        self.page_factory_output: scrolledtext.ScrolledText
        self.capture_review_var = StringVar(value="No pending browser capture.")
        self.js_editor: scrolledtext.ScrolledText
        self.js_output: scrolledtext.ScrolledText
        self.catalog_output: scrolledtext.ScrolledText
        self.batch_urls: scrolledtext.ScrolledText
        self.generator_output: scrolledtext.ScrolledText
        self.generator_list_output: scrolledtext.ScrolledText
        self.gherkin_output: scrolledtext.ScrolledText
        self.custom_steps_output: scrolledtext.ScrolledText
        self.runner_editor: scrolledtext.ScrolledText
        self.runner_log_output: scrolledtext.ScrolledText

        self._build_ui()
        for variable in (
            self.page_class_var,
            self.steps_class_var,
            self.step_def_class_var,
            self.package_var,
            self.generator_page_url_var,
            self.queue_filter_var,
            self.output_dir_var,
            self.runner_timeout_var,
            self.runner_step_delay_var,
        ):
            variable.trace_add("write", lambda *_args: self._on_state_var_changed())
        self._bind_text_dirty_tracking()
        self._load_settings()
        self._load_last_project_if_available()
        self._refresh_generator_outputs()
        self._clear_dirty()
        self.root.bind_all("<Control-s>", lambda _event: self.save_project_state())
        self.root.bind_all("<Control-o>", lambda _event: self.load_project_state())
        self.root.bind_all("<Control-g>", lambda _event: self.generate_code_files())
        self.root.bind_all("<F5>", lambda _event: self.run_gherkin_scenario())
        self._poll_messages()
        self._poll_browser_info()
        self._poll_browser_picker()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        container = ttk.Frame(self.root, padding=12)
        container.pack(fill=BOTH, expand=True)

        top = ttk.Frame(container)
        top.pack(fill="x")

        ttk.Label(top, text="Address").pack(side=LEFT)
        address = ttk.Entry(top, textvariable=self.url_var)
        address.pack(side=LEFT, fill="x", expand=True, padx=(8, 8))
        address.bind("<Return>", lambda _event: self.open_url())

        ttk.Button(top, text="Launch Browser", command=self.launch_browser).pack(side=LEFT)
        ttk.Button(top, text="Open URL", command=self.open_url).pack(side=LEFT, padx=(8, 0))
        ttk.Button(top, text="Page Summary", command=self.load_page_summary).pack(side=LEFT, padx=(8, 0))
        ttk.Button(top, text="Element Catalog", command=self.load_locator_catalog).pack(side=LEFT, padx=(8, 0))

        info = ttk.Frame(container)
        info.pack(fill="x", pady=(10, 6))
        ttk.Label(info, textvariable=self.title_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.current_url_var, wraplength=1020).pack(anchor="w", pady=(4, 0))
        ttk.Label(info, textvariable=self.status_var, wraplength=1020).pack(anchor="w", pady=(4, 0))

        utility = ttk.Frame(container)
        utility.pack(fill="x", pady=(0, 8))
        ttk.Button(utility, text="Save Project", command=self.save_project_state).pack(side=LEFT)
        ttk.Button(utility, text="Load Project", command=self.load_project_state).pack(side=LEFT, padx=(8, 0))
        ttk.Button(utility, text="Open Output Folder", command=self.open_output_folder).pack(side=LEFT, padx=(8, 0))
        ttk.Button(utility, text="Open Runner Artifacts", command=self.open_runner_artifacts_folder).pack(side=LEFT, padx=(8, 0))
        ttk.Button(utility, text="Open Log", command=self.open_log_file).pack(side=LEFT, padx=(8, 0))
        ttk.Button(utility, text="About", command=self.show_about).pack(side=RIGHT)

        notebook = ttk.Notebook(container)
        notebook.pack(fill=BOTH, expand=True, pady=(8, 0))

        save_tab = ttk.Frame(notebook, padding=12)
        locators_tab = ttk.Frame(notebook, padding=12)
        automation_tab = ttk.Frame(notebook, padding=12)
        batch_tab = ttk.Frame(notebook, padding=12)
        generator_tab = ttk.Frame(notebook, padding=12)
        runner_tab = ttk.Frame(notebook, padding=12)
        help_tab = ttk.Frame(notebook, padding=12)

        notebook.add(save_tab, text="Save && Capture")
        notebook.add(locators_tab, text="Locators && POM")
        notebook.add(automation_tab, text="Automation")
        notebook.add(batch_tab, text="Batch")
        notebook.add(generator_tab, text="Code Generator")
        notebook.add(runner_tab, text="Scenario Runner")
        notebook.add(help_tab, text="Help")

        self._build_save_tab(save_tab)
        self._build_locators_tab(locators_tab)
        self._build_automation_tab(automation_tab)
        self._build_batch_tab(batch_tab)
        self._build_generator_tab(generator_tab)
        self._build_runner_tab(runner_tab)
        self._build_help_tab(help_tab)

    def _build_save_tab(self, parent: ttk.Frame) -> None:
        actions = ttk.Frame(parent)
        actions.pack(fill="x")
        ttk.Button(actions, text="Save Current Page", command=self.save_page).pack(side=LEFT)
        ttk.Button(actions, text="Viewport Screenshot", command=self.save_viewport_screenshot).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Full Page Screenshot", command=self.save_full_page_screenshot).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Export Cookies JSON", command=self.export_cookies).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Export Storage JSON", command=self.export_storage).pack(side=LEFT, padx=(8, 0))

        note = (
            "Features added for test work: page save, two screenshot modes, cookie export, and storage export. "
            "These are useful for debugging state before writing or fixing Selenium flows."
        )
        ttk.Label(parent, text=note, wraplength=980).pack(anchor="w", pady=(18, 0))

    def _build_locators_tab(self, parent: ttk.Frame) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x")
        ttk.Label(row, text="CSS Selector").pack(side=LEFT)
        selector_entry = ttk.Entry(row, textvariable=self.selector_var)
        selector_entry.pack(side=LEFT, fill="x", expand=True, padx=(8, 8))
        selector_entry.bind("<Return>", lambda _event: self.inspect_selector())

        ttk.Button(row, text="Inspect", command=self.inspect_selector).pack(side=LEFT)
        ttk.Button(row, text="Highlight", command=self.highlight_selector).pack(side=LEFT, padx=(8, 0))
        ttk.Button(row, text="Enable Browser Picker", command=self.enable_browser_picker).pack(side=LEFT, padx=(8, 0))
        ttk.Button(row, text="Copy PageFactory", command=lambda: self.copy_text_widget(self.page_factory_output)).pack(side=LEFT, padx=(8, 0))
        ttk.Button(row, text="Add To Generator", command=self.add_inspected_to_generator).pack(side=LEFT, padx=(8, 0))

        review = ttk.Frame(parent)
        review.pack(fill="x", pady=(10, 0))
        ttk.Label(review, textvariable=self.capture_review_var, wraplength=980).pack(side=LEFT, fill="x", expand=True)
        ttk.Button(review, text="Accept Capture", command=self.accept_pending_capture).pack(side=LEFT, padx=(8, 0))
        ttk.Button(review, text="Discard Capture", command=self.discard_pending_capture).pack(side=LEFT, padx=(8, 0))

        split = ttk.Panedwindow(parent, orient="horizontal")
        split.pack(fill=BOTH, expand=True, pady=(12, 0))

        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(right, weight=1)

        ttk.Label(left, text="Inspection Result").pack(anchor="w")
        self.inspect_output = scrolledtext.ScrolledText(left, wrap="word", height=20)
        self.inspect_output.pack(fill=BOTH, expand=True, pady=(6, 0))

        ttk.Label(right, text="Java PageFactory Snippet").pack(anchor="w")
        self.page_factory_output = scrolledtext.ScrolledText(right, wrap="word", height=20)
        self.page_factory_output.pack(fill=BOTH, expand=True, pady=(6, 0))

    def _build_automation_tab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent)
        top.pack(fill="x")
        ttk.Label(top, text="Wait Timeout (ms)").pack(side=LEFT)
        ttk.Entry(top, textvariable=self.wait_timeout_var, width=12).pack(side=LEFT, padx=(8, 12))
        ttk.Button(top, text="Wait For Selector", command=self.wait_for_selector).pack(side=LEFT)

        ttk.Label(top, text="  JavaScript").pack(side=LEFT, padx=(18, 8))
        ttk.Button(top, text="Run Script", command=self.run_javascript).pack(side=LEFT)

        body = ttk.Panedwindow(parent, orient="horizontal")
        body.pack(fill=BOTH, expand=True, pady=(12, 0))

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=1)

        ttk.Label(left, text="Script Editor").pack(anchor="w")
        self.js_editor = scrolledtext.ScrolledText(left, wrap="word", height=18)
        self.js_editor.pack(fill=BOTH, expand=True, pady=(6, 0))
        self.js_editor.insert("1.0", "return {\n  title: document.title,\n  activeElement: document.activeElement?.outerHTML || null\n};")

        ttk.Label(right, text="Output").pack(anchor="w")
        self.js_output = scrolledtext.ScrolledText(right, wrap="word", height=18)
        self.js_output.pack(fill=BOTH, expand=True, pady=(6, 0))

        ttk.Label(parent, text="Locator Catalog").pack(anchor="w", pady=(12, 0))
        self.catalog_output = scrolledtext.ScrolledText(parent, wrap="word", height=10)
        self.catalog_output.pack(fill=BOTH, expand=True, pady=(6, 0))

    def _build_batch_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Enter one URL per line. The app will navigate and save each page into the folder you choose.").pack(anchor="w")
        self.batch_urls = scrolledtext.ScrolledText(parent, wrap="word", height=18)
        self.batch_urls.pack(fill=BOTH, expand=True, pady=(8, 0))
        self.batch_urls.insert("1.0", "https://example.com\nhttps://www.wikipedia.org")

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Batch Save URLs", command=self.batch_save_urls).pack(side=LEFT)
        ttk.Label(actions, textvariable=self.batch_status_var).pack(side=LEFT, padx=(12, 0))

    def _build_generator_tab(self, parent: ttk.Frame) -> None:
        row1 = ttk.Frame(parent)
        row1.pack(fill="x")
        ttk.Label(row1, text="Output Folder").pack(side=LEFT)
        ttk.Entry(row1, textvariable=self.output_dir_var).pack(side=LEFT, fill="x", expand=True, padx=(8, 8))
        ttk.Button(row1, text="Browse", command=self.pick_output_dir).pack(side=LEFT)
        ttk.Label(row1, text="  Page URL").pack(side=LEFT, padx=(16, 6))
        ttk.Entry(row1, textvariable=self.generator_page_url_var, width=40).pack(side=LEFT, padx=(0, 8))
        ttk.Button(row1, text="Use Current URL", command=self.use_current_page_url_for_generator).pack(side=LEFT)

        row2 = ttk.Frame(parent)
        row2.pack(fill="x", pady=(10, 0))
        ttk.Label(row2, text="Package").pack(side=LEFT)
        ttk.Entry(row2, textvariable=self.package_var, width=32).pack(side=LEFT, padx=(8, 16))
        ttk.Label(row2, text="Filter Queue").pack(side=LEFT)
        ttk.Entry(row2, textvariable=self.queue_filter_var, width=18).pack(side=LEFT, padx=(8, 16))
        ttk.Label(row2, text="Page Class").pack(side=LEFT)
        ttk.Entry(row2, textvariable=self.page_class_var, width=22).pack(side=LEFT, padx=(8, 16))
        ttk.Label(row2, text="Steps Class").pack(side=LEFT)
        ttk.Entry(row2, textvariable=self.steps_class_var, width=22).pack(side=LEFT, padx=(8, 16))
        ttk.Label(row2, text="StepDefs Class").pack(side=LEFT)
        ttk.Entry(row2, textvariable=self.step_def_class_var, width=24).pack(side=LEFT, padx=(8, 0))

        actions = ttk.Frame(parent)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Import Saved HTML", command=self.import_saved_html).pack(side=LEFT)
        ttk.Button(actions, text="Generate Files", command=self.generate_code_files).pack(side=LEFT)
        ttk.Button(actions, text="Clear Generator List", command=self.clear_generator_list).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Rename Selected", command=self.rename_selected_generator_element).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Delete Selected", command=self.delete_selected_generator_element).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Move Up", command=lambda: self.move_selected_generator_element(-1)).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Move Down", command=lambda: self.move_selected_generator_element(1)).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Copy Preview", command=lambda: self.copy_text_widget(self.generator_output)).pack(side=LEFT, padx=(8, 0))
        ttk.Button(actions, text="Copy Gherkin", command=lambda: self.copy_text_widget(self.gherkin_output)).pack(side=LEFT, padx=(8, 0))

        builder = ttk.Frame(parent)
        builder.pack(fill="x", pady=(10, 0))
        ttk.Label(builder, text="Queued Element").pack(side=LEFT)
        self.step_target_combo = ttk.Combobox(builder, textvariable=self.step_target_var, width=28, state="readonly")
        self.step_target_combo.pack(side=LEFT, padx=(8, 12))
        ttk.Label(builder, text="Step").pack(side=LEFT)
        self.step_template_combo = ttk.Combobox(
            builder,
            textvariable=self.step_template_var,
            width=26,
            state="readonly",
            values=[
                "Click Element",
                "Hover Element",
                "Wait Visible",
                "Wait Disappear",
                "Text Equals",
                "Text Contains",
                "Enter Text",
                "Enter Date",
                "Upload File",
                "Select Dropdown",
                "Check Checkbox",
                "Uncheck Checkbox",
                "Select Radio",
                "Press Key On Element",
                "Wait Text Appear",
            ],
        )
        self.step_template_combo.pack(side=LEFT, padx=(8, 12))
        ttk.Label(builder, text="Value").pack(side=LEFT)
        ttk.Entry(builder, textvariable=self.step_value_var, width=28).pack(side=LEFT, padx=(8, 8))
        ttk.Button(builder, text="Add Step", command=self.add_custom_step_for_element).pack(side=LEFT)
        ttk.Button(builder, text="Clear Steps", command=self.clear_custom_steps).pack(side=LEFT, padx=(8, 0))

        split = ttk.Panedwindow(parent, orient="horizontal")
        split.pack(fill=BOTH, expand=True, pady=(12, 0))

        left = ttk.Frame(split)
        center = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(center, weight=1)
        split.add(right, weight=1)

        ttk.Label(left, text="Queued Elements").pack(anchor="w")
        self.generator_list_output = scrolledtext.ScrolledText(left, wrap="word", height=20)
        self.generator_list_output.pack(fill=BOTH, expand=True, pady=(6, 0))
        ttk.Label(left, text="Custom Scenario Steps").pack(anchor="w", pady=(10, 0))
        self.custom_steps_output = scrolledtext.ScrolledText(left, wrap="word", height=10)
        self.custom_steps_output.pack(fill=BOTH, expand=True, pady=(6, 0))

        ttk.Label(center, text="Generated Preview").pack(anchor="w")
        self.generator_output = scrolledtext.ScrolledText(center, wrap="word", height=20)
        self.generator_output.pack(fill=BOTH, expand=True, pady=(6, 0))

        ttk.Label(right, text="Gherkin Steps").pack(anchor="w")
        self.gherkin_output = scrolledtext.ScrolledText(right, wrap="word", height=20)
        self.gherkin_output.pack(fill=BOTH, expand=True, pady=(6, 0))

    def _build_runner_tab(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent)
        controls.pack(fill="x")
        ttk.Button(controls, text="Load Generated Gherkin", command=self.load_generated_gherkin_into_runner).pack(side=LEFT)
        ttk.Button(controls, text="Open Feature File", command=self.open_feature_file_into_runner).pack(side=LEFT, padx=(8, 0))
        ttk.Button(controls, text="Run Scenario", command=self.run_gherkin_scenario).pack(side=LEFT, padx=(8, 0))
        ttk.Button(controls, text="Copy Log", command=lambda: self.copy_text_widget(self.runner_log_output)).pack(side=LEFT, padx=(8, 0))
        ttk.Label(controls, text="Wait (ms)").pack(side=LEFT, padx=(20, 6))
        ttk.Entry(controls, textvariable=self.runner_timeout_var, width=10).pack(side=LEFT)
        ttk.Label(controls, text="Step Delay (ms)").pack(side=LEFT, padx=(16, 6))
        ttk.Entry(controls, textvariable=self.runner_step_delay_var, width=10).pack(side=LEFT)
        ttk.Label(controls, textvariable=self.runner_status_var).pack(side=RIGHT)

        body = ttk.Panedwindow(parent, orient="horizontal")
        body.pack(fill=BOTH, expand=True, pady=(12, 0))

        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=1)
        body.add(right, weight=1)

        ttk.Label(left, text="Feature / Gherkin").pack(anchor="w")
        self.runner_editor = scrolledtext.ScrolledText(left, wrap="word", height=24)
        self.runner_editor.pack(fill=BOTH, expand=True, pady=(6, 0))

        ttk.Label(right, text="Execution Log").pack(anchor="w")
        self.runner_log_output = scrolledtext.ScrolledText(right, wrap="word", height=24)
        self.runner_log_output.pack(fill=BOTH, expand=True, pady=(6, 0))

    def _build_help_tab(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent)
        header.pack(fill="x")
        if hasattr(self, "logo_image"):
            ttk.Label(header, image=self.logo_image).pack(side=LEFT, padx=(0, 8))
        ttk.Label(header, text=f"{APP_NAME} {APP_VERSION}").pack(side=LEFT)

        help_text = scrolledtext.ScrolledText(parent, wrap="word", height=30)
        help_text.pack(fill=BOTH, expand=True, pady=(10, 0))
        help_text.insert(
            "1.0",
            "\n".join(
                [
                    "Quick Start",
                    "1. Launch Browser and browse to the page you want to save or automate.",
                    "2. Use Save & Capture to save HTML, screenshots, cookies, and storage.",
                    "3. Use Locators & POM to inspect or browser-pick elements, then accept captures into the queue.",
                    "4. Use Code Generator to import saved HTML, manage queued locators, add custom steps, and generate Java plus Gherkin.",
                    "5. Use Scenario Runner to execute generated or pasted Gherkin against the live browser.",
                    "",
                    "Supported Runner Actions",
                    "- Open URL, resize browser, clear cookies/storage",
                    "- Switch iframe, main content, and newest window",
                    "- Click, hover, type, press keys, drag and drop",
                    "- Select dropdowns, toggle checkboxes, select radio buttons",
                    "- Upload files and set date inputs",
                    "- Wait for visibility, disappearance, URL changes, and text changes",
                    "- Assert titles, text, values, selection state, table rows, toasts, and modals",
                    "",
                    "Project Workflow",
                    "- Save Project stores queue, custom steps, runner text, waits, and output settings as JSON.",
                    "- Load Project restores that workspace.",
                    "- The app also remembers your last project, output folder, and runner artifact folder automatically.",
                    "",
                    "Packaging",
                    "- Run .\\build.ps1 to create a Windows executable with PyInstaller.",
                    "- The packaged app still requires Playwright Chromium to be installed on the target machine.",
                ]
            ),
        )
        help_text.configure(state="disabled")

    def _poll_messages(self) -> None:
        try:
            while True:
                level, message = self.messages.get_nowait()
                if level == "error":
                    logging.error(message)
                    messagebox.showerror(APP_NAME, message)
                elif level == "info":
                    logging.info(message)
                    messagebox.showinfo(APP_NAME, message)
                else:
                    logging.info("%s: %s", level, message)
                self.status_var.set(f"Status: {message}")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_messages)

    def _poll_browser_info(self) -> None:
        future = self.worker.run(self.worker.current_info())
        future.add_done_callback(self._handle_browser_info)
        self.root.after(1500, self._poll_browser_info)

    def _poll_browser_picker(self) -> None:
        future = self.worker.run(self.worker.get_pending_browser_pick())
        future.add_done_callback(self._handle_browser_picker_result)
        self.root.after(700, self._poll_browser_picker)

    def _handle_browser_info(self, future) -> None:
        try:
            info = future.result()
        except Exception:
            return
        self.root.after(0, lambda: self._apply_browser_info(info))

    def _apply_browser_info(self, info: Dict[str, str]) -> None:
        self.title_var.set(f"Title: {info.get('title', '')}")
        self.current_url_var.set(f"Current URL: {info.get('url', '')}")
        self.status_var.set(f"Status: {info.get('status', '')}")

    def launch_browser(self) -> None:
        target = self.url_var.get().strip()
        self.status_var.set("Status: Launching browser...")
        future = self.worker.run(self.worker.launch_browser(target or None))
        success_message = f"Browser launched and loaded {target}" if target else "Browser launched."
        future.add_done_callback(self._notify_result(success_message))

    def open_url(self) -> None:
        target = self.url_var.get().strip()
        if not target:
            return
        self.status_var.set("Status: Opening page...")
        future = self.worker.run(self.worker.navigate(target))
        future.add_done_callback(self._notify_result(f"Loaded {target}"))

    def save_page(self) -> None:
        destination = filedialog.asksaveasfilename(
            title="Save Current Page",
            defaultextension=".html",
            initialfile=self._default_filename(".html"),
            filetypes=[("HTML files", "*.html"), ("All files", "*.*")],
        )
        if not destination:
            return
        self.status_var.set("Status: Saving current page...")
        future = self.worker.run(self.worker.save_current_page(Path(destination)))
        future.add_done_callback(self._message_result("Saved HTML file."))

    def save_viewport_screenshot(self) -> None:
        destination = self._pick_png("Save Viewport Screenshot")
        if not destination:
            return
        self.status_var.set("Status: Capturing viewport screenshot...")
        future = self.worker.run(self.worker.save_viewport_screenshot(destination))
        future.add_done_callback(self._message_result(f"Saved screenshot to {destination.name}"))

    def save_full_page_screenshot(self) -> None:
        destination = self._pick_png("Save Full Page Screenshot")
        if not destination:
            return
        self.status_var.set("Status: Capturing full page screenshot...")
        future = self.worker.run(self.worker.save_full_page_screenshot(destination))
        future.add_done_callback(self._message_result(f"Saved full page screenshot to {destination.name}"))

    def export_cookies(self) -> None:
        destination = self._pick_json("Export Cookies", "cookies")
        if not destination:
            return
        future = self.worker.run(self.worker.export_cookies(destination))
        future.add_done_callback(lambda f: self._handle_count_result(f, "cookies exported"))

    def export_storage(self) -> None:
        destination = self._pick_json("Export Storage", "storage")
        if not destination:
            return
        future = self.worker.run(self.worker.export_storage(destination))
        future.add_done_callback(self._handle_storage_result)

    def inspect_selector(self) -> None:
        selector = self.selector_var.get().strip()
        if not selector:
            return
        self.status_var.set("Status: Inspecting selector...")
        future = self.worker.run(self.worker.inspect_selector(selector))
        future.add_done_callback(self._handle_inspect_result)

    def enable_browser_picker(self) -> None:
        self.status_var.set("Status: Browser picker enabled. Click the floating inspect button in Chromium or this will arm it directly.")
        future = self.worker.run(self.worker.enable_browser_picker())
        future.add_done_callback(self._message_result("Browser picker is active. Click an element in Chromium."))

    def add_inspected_to_generator(self) -> None:
        if not self.inspected_element:
            self.messages.put(("error", "Inspect an element first, then add it to the generator."))
            return
        if not self.generator_page_url_var.get().strip():
            self.use_current_page_url_for_generator()
        self._append_generator_element(self.inspected_element)
        self._refresh_generator_outputs()
        self.messages.put(("info", f'Added "{self.generator_elements[-1]["field_name"]}" to generator list.'))

    def accept_pending_capture(self) -> None:
        if not self.pending_capture_element:
            self.messages.put(("error", "There is no pending browser capture to accept."))
            return
        self.inspected_element = self.pending_capture_element
        if not self.generator_page_url_var.get().strip():
            self.use_current_page_url_for_generator()
        self._append_generator_element(self.pending_capture_element)
        added_name = self.generator_elements[-1]["field_name"]
        self.pending_capture_element = None
        self.capture_review_var.set("No pending browser capture.")
        self._refresh_generator_outputs()
        self.messages.put(("info", f'Accepted browser capture and added "{added_name}" to generator list.'))

    def discard_pending_capture(self) -> None:
        self.pending_capture_element = None
        self.capture_review_var.set("No pending browser capture.")
        self.messages.put(("info", "Discarded pending browser capture."))

    def highlight_selector(self) -> None:
        selector = self.selector_var.get().strip()
        if not selector:
            return
        future = self.worker.run(self.worker.highlight_selector(selector))
        future.add_done_callback(lambda f: self._handle_count_result(f, "elements highlighted"))

    def wait_for_selector(self) -> None:
        selector = self.selector_var.get().strip()
        if not selector:
            return
        try:
            timeout_ms = int(self.wait_timeout_var.get().strip())
        except ValueError:
            messagebox.showerror(APP_NAME, "Wait timeout must be a whole number of milliseconds.")
            return
        self.status_var.set("Status: Waiting for selector...")
        future = self.worker.run(self.worker.wait_for_selector(selector, timeout_ms))
        future.add_done_callback(self._handle_inspect_result)

    def run_javascript(self) -> None:
        script = self.js_editor.get("1.0", "end").strip()
        if not script:
            return
        self.status_var.set("Status: Running JavaScript...")
        future = self.worker.run(self.worker.run_javascript(script))
        future.add_done_callback(self._handle_js_output)

    def load_page_summary(self) -> None:
        future = self.worker.run(self.worker.page_summary())
        future.add_done_callback(self._handle_page_summary)

    def load_locator_catalog(self) -> None:
        future = self.worker.run(self.worker.locator_catalog())
        future.add_done_callback(self._handle_catalog_result)

    def batch_save_urls(self) -> None:
        raw = self.batch_urls.get("1.0", "end").strip()
        urls = [line.strip() for line in raw.splitlines() if line.strip()]
        if not urls:
            return
        destination = filedialog.askdirectory(title="Select Output Folder")
        if not destination:
            return
        self.batch_status_var.set("Batch: running")
        future = self.worker.run(self.worker.batch_save_pages(urls, Path(destination)))
        future.add_done_callback(self._handle_batch_result)

    def pick_output_dir(self) -> None:
        destination = filedialog.askdirectory(title="Select Java Output Folder")
        if destination:
            self.output_dir_var.set(destination)

    def use_current_page_url_for_generator(self) -> None:
        current = self.current_url_var.get().replace("Current URL: ", "").strip()
        if current:
            self.generator_page_url_var.set(current)
            self._refresh_generator_outputs()
            self._mark_dirty()

    def rename_selected_generator_element(self) -> None:
        selected_name = self.step_target_var.get().strip()
        if not selected_name:
            self.messages.put(("error", "Choose a queued element to rename."))
            return
        for element in self.generator_elements:
            if element["field_name"] == selected_name:
                new_name = sanitize_filename(self.step_value_var.get().strip(), fallback=selected_name, max_length=60).replace(" ", "")
                if not new_name:
                    self.messages.put(("error", "Enter a new field name in the Value box first."))
                    return
                if any(item["field_name"] == new_name for item in self.generator_elements if item is not element):
                    self.messages.put(("error", f'Another queued element already uses "{new_name}".'))
                    return
                element["field_name"] = new_name
                self.step_target_var.set(new_name)
                self._mark_dirty()
                self._refresh_generator_outputs()
                self.messages.put(("info", f'Renamed queued element to "{new_name}".'))
                return
        self.messages.put(("error", "Selected queued element was not found."))

    def delete_selected_generator_element(self) -> None:
        selected_name = self.step_target_var.get().strip()
        if not selected_name:
            self.messages.put(("error", "Choose a queued element to delete."))
            return
        original_count = len(self.generator_elements)
        self.generator_elements = [element for element in self.generator_elements if element["field_name"] != selected_name]
        if len(self.generator_elements) == original_count:
            self.messages.put(("error", "Selected queued element was not found."))
            return
        self._mark_dirty()
        self._refresh_generator_outputs()
        self.messages.put(("info", f'Deleted queued element "{selected_name}".'))

    def move_selected_generator_element(self, direction: int) -> None:
        selected_name = self.step_target_var.get().strip()
        if not selected_name:
            self.messages.put(("error", "Choose a queued element to move."))
            return
        for index, element in enumerate(self.generator_elements):
            if element["field_name"] == selected_name:
                new_index = index + direction
                if new_index < 0 or new_index >= len(self.generator_elements):
                    return
                self.generator_elements[index], self.generator_elements[new_index] = self.generator_elements[new_index], self.generator_elements[index]
                self._mark_dirty()
                self._refresh_generator_outputs()
                self.messages.put(("info", f'Moved "{selected_name}".'))
                return
        self.messages.put(("error", "Selected queued element was not found."))

    def clear_generator_list(self) -> None:
        self.generator_elements.clear()
        self.custom_gherkin_steps.clear()
        self.pending_capture_element = None
        self.capture_review_var.set("No pending browser capture.")
        self._mark_dirty()
        self._refresh_generator_outputs()
        self.messages.put(("info", "Generator list cleared."))

    def import_saved_html(self) -> None:
        source = filedialog.askopenfilename(
            title="Import Saved HTML Page",
            filetypes=[("HTML files", "*.html;*.htm"), ("All files", "*.*")],
        )
        if not source:
            return

        html = Path(source).read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "html.parser")
        imported_url = ""
        meta_original = soup.find("meta", attrs={"name": "save-page-desktop-original-url"})
        if meta_original and meta_original.get("content"):
            imported_url = meta_original["content"].strip()
        if not imported_url:
            imported_url = Path(source).resolve().as_uri()
        self.generator_page_url_var.set(imported_url)
        nodes = soup.select(
            "input, textarea, select, button, a, table, dialog, [role='dialog'], [role='alert'], [role='status'], [class*='toast'], [class*='snackbar']"
        )
        if not nodes:
            self.messages.put(("error", "No supported elements were found in the imported HTML file."))
            return

        added = 0
        for node in nodes:
            details = self._build_imported_element_details(node)
            if not details.get("locators"):
                continue
            self._append_generator_element(details)
            added += 1

        self._refresh_generator_outputs()
        self._mark_dirty()
        self.messages.put(("info", f"Imported {added} elements from saved HTML."))

    def add_custom_step_for_element(self) -> None:
        target_name = self.step_target_var.get().strip()
        template = self.step_template_var.get().strip()
        value = self.step_value_var.get().strip()
        if not target_name:
            self.messages.put(("error", "Choose a queued element for the custom step."))
            return
        readable = readable_field_name(target_name)
        step_line = self._build_custom_step_line(template, readable, value)
        if not step_line:
            return
        self.custom_gherkin_steps.append(step_line)
        self._mark_dirty()
        self._refresh_generator_outputs()
        self.messages.put(("info", "Added custom Gherkin step."))

    def clear_custom_steps(self) -> None:
        self.custom_gherkin_steps.clear()
        self._mark_dirty()
        self._refresh_generator_outputs()
        self.messages.put(("info", "Custom Gherkin steps cleared."))

    def _build_custom_step_line(self, template: str, readable: str, value: str) -> Optional[str]:
        if template in {"Text Equals", "Text Contains", "Enter Text", "Enter Date", "Upload File", "Select Dropdown", "Press Key On Element", "Wait Text Appear"} and not value:
            self.messages.put(("error", f'Value is required for "{template}".'))
            return None
        mapping = {
            "Click Element": f"When I click the {readable} element",
            "Hover Element": f"When I hover over the {readable} element",
            "Wait Visible": f"Then the {readable} element should be visible",
            "Wait Disappear": f"Then I wait for the {readable} element to disappear",
            "Text Equals": f'Then the {readable} element text should be "{value}"',
            "Text Contains": f'Then the {readable} element text should contain "{value}"',
            "Enter Text": f'When I enter "{value}" into the {readable} field',
            "Enter Date": f'When I enter date "{value}" into the {readable} field',
            "Upload File": f'When I upload file "{value}" into the {readable} field',
            "Select Dropdown": f'When I select "{value}" from the {readable} dropdown',
            "Check Checkbox": f'When I set the {readable} checkbox to "checked"',
            "Uncheck Checkbox": f'When I set the {readable} checkbox to "unchecked"',
            "Select Radio": f"When I select the {readable} radio option",
            "Press Key On Element": f'When I press key "{value}" on the {readable} element',
            "Wait Text Appear": f'Then I wait for text "{value}" to appear in the {readable} element',
        }
        return mapping.get(template)

    def generate_code_files(self) -> None:
        if not self.generator_elements:
            self.messages.put(("error", "Add at least one inspected element to the generator list."))
            return
        output_dir_text = self.output_dir_var.get().strip()
        if not output_dir_text:
            self.messages.put(("error", "Choose an output folder for generated files."))
            return
        output_dir = Path(output_dir_text)
        page_class = java_class_name(self.page_class_var.get().strip(), "GeneratedPage")
        steps_class = java_class_name(self.steps_class_var.get().strip(), "GeneratedSteps")
        step_def_class = java_class_name(self.step_def_class_var.get().strip(), "GeneratedStepDefinitions")
        package_name = self.package_var.get().strip() or "com.example.automation"

        artifacts = self._build_generator_artifacts(package_name, page_class, steps_class, step_def_class)
        java_root = output_dir / "java" / Path(*package_name.split("."))
        features_root = output_dir / "features"
        pages_root = java_root / "pages"
        steps_root = java_root / "steps"
        step_defs_root = java_root / "stepdefinitions"
        for folder in (pages_root, steps_root, step_defs_root, features_root):
            folder.mkdir(parents=True, exist_ok=True)
        files = {
            pages_root / f"{page_class}.java": artifacts["page_class"],
            steps_root / f"{steps_class}.java": artifacts["steps_class"],
            step_defs_root / f"{step_def_class}.java": artifacts["step_definitions_class"],
            features_root / f"{page_class}.feature": artifacts["gherkin"],
        }
        for filepath, content in files.items():
            filepath.write_text(content, encoding="utf-8")
        self.last_output_dir = output_dir
        self._write_project_state(DEFAULT_PROJECT_PATH)
        self.last_project_path = DEFAULT_PROJECT_PATH
        self._save_settings()
        self._set_text(self.generator_output, artifacts["combined_preview"])
        self._set_text(self.gherkin_output, artifacts["gherkin"])
        if not self.runner_editor.get("1.0", "end").strip():
            self._set_text(self.runner_editor, artifacts["gherkin"])
        self.messages.put(("info", f"Generated {len(files)} files under {output_dir}"))

    def load_generated_gherkin_into_runner(self) -> None:
        text = self.gherkin_output.get("1.0", "end").strip()
        if not text:
            self.messages.put(("error", "No generated Gherkin is available yet."))
            return
        self._set_text(self.runner_editor, text)
        self.runner_status_var.set("Runner: generated Gherkin loaded")
        self._mark_dirty()

    def open_feature_file_into_runner(self) -> None:
        source = filedialog.askopenfilename(
            title="Open Feature File",
            filetypes=[("Feature files", "*.feature;*.txt"), ("All files", "*.*")],
        )
        if not source:
            return
        content = Path(source).read_text(encoding="utf-8", errors="replace")
        self._set_text(self.runner_editor, content)
        self.runner_status_var.set(f"Runner: loaded {Path(source).name}")
        self._mark_dirty()

    def run_gherkin_scenario(self) -> None:
        feature_text = self.runner_editor.get("1.0", "end").strip()
        if not feature_text:
            self.messages.put(("error", "Paste or load Gherkin into the Scenario Runner first."))
            return
        if not self.generator_elements:
            self.messages.put(("error", "Queue locator elements first. The runner uses the current generator locators to resolve steps."))
            return
        try:
            timeout_ms = int(self.runner_timeout_var.get().strip())
            step_delay_ms = int(self.runner_step_delay_var.get().strip())
        except ValueError:
            self.messages.put(("error", "Runner wait and step delay values must be whole numbers."))
            return

        try:
            parsed_steps = parse_generated_gherkin(feature_text, self.generator_elements)
        except Exception as exc:
            self.messages.put(("error", str(exc)))
            return

        self.runner_status_var.set("Runner: executing")
        self._set_text(self.runner_log_output, "")
        future = self.worker.run(self.worker.run_gherkin_steps(parsed_steps, timeout_ms, step_delay_ms))
        future.add_done_callback(self._handle_runner_result)

    def copy_text_widget(self, widget: scrolledtext.ScrolledText) -> None:
        text = widget.get("1.0", "end").strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.messages.put(("info", "Copied text to clipboard."))

    def _bind_text_dirty_tracking(self) -> None:
        for widget in (
            self.runner_editor,
            self.generator_output,
            self.gherkin_output,
            self.custom_steps_output,
            self.generator_list_output,
            self.inspect_output,
            self.page_factory_output,
            self.runner_log_output,
        ):
            widget.bind("<<Modified>>", self._on_text_modified)

    def _on_text_modified(self, event) -> None:
        widget = event.widget
        if widget.edit_modified():
            widget.edit_modified(False)
            if widget in (self.runner_editor,):
                self._mark_dirty()

    def _on_state_var_changed(self) -> None:
        self._mark_dirty()
        self._refresh_generator_outputs()

    def _mark_dirty(self) -> None:
        if not self.is_dirty:
            self.is_dirty = True
            self.root.title(f"{APP_NAME} {APP_VERSION} *")

    def _clear_dirty(self) -> None:
        self.is_dirty = False
        self.root.title(f"{APP_NAME} {APP_VERSION}")

    def _project_payload(self) -> Dict[str, Any]:
        return {
            "version": APP_VERSION,
            "saved_at": datetime.now().isoformat(),
            "output_dir": self.output_dir_var.get().strip(),
            "package_name": self.package_var.get().strip(),
            "page_class": self.page_class_var.get().strip(),
            "steps_class": self.steps_class_var.get().strip(),
            "step_def_class": self.step_def_class_var.get().strip(),
            "page_url": self.generator_page_url_var.get().strip(),
            "queue_filter": self.queue_filter_var.get().strip(),
            "generator_elements": self.generator_elements,
            "custom_gherkin_steps": self.custom_gherkin_steps,
            "runner_text": self.runner_editor.get("1.0", "end").strip(),
            "runner_timeout_ms": self.runner_timeout_var.get().strip(),
            "runner_step_delay_ms": self.runner_step_delay_var.get().strip(),
            "last_output_dir": str(self.last_output_dir) if self.last_output_dir else "",
            "last_runner_artifact_dir": str(self.last_runner_artifact_dir) if self.last_runner_artifact_dir else "",
        }

    def _write_project_state(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._project_payload(), indent=2), encoding="utf-8")

    def _read_project_state(self, path: Path) -> None:
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.output_dir_var.set(payload.get("output_dir", ""))
        self.package_var.set(payload.get("package_name", "com.example.automation"))
        self.page_class_var.set(payload.get("page_class", "LoginPage"))
        self.steps_class_var.set(payload.get("steps_class", "LoginSteps"))
        self.step_def_class_var.set(payload.get("step_def_class", "LoginStepDefinitions"))
        self.generator_page_url_var.set(payload.get("page_url", ""))
        self.queue_filter_var.set(payload.get("queue_filter", ""))
        self.generator_elements = payload.get("generator_elements", [])
        self.custom_gherkin_steps = payload.get("custom_gherkin_steps", [])
        self.runner_timeout_var.set(payload.get("runner_timeout_ms", "10000"))
        self.runner_step_delay_var.set(payload.get("runner_step_delay_ms", "150"))
        self.last_output_dir = Path(payload["last_output_dir"]) if payload.get("last_output_dir") else None
        self.last_runner_artifact_dir = Path(payload["last_runner_artifact_dir"]) if payload.get("last_runner_artifact_dir") else None
        self._set_text(self.runner_editor, payload.get("runner_text", ""))
        self.capture_review_var.set("No pending browser capture.")
        self.pending_capture_element = None
        self._refresh_generator_outputs()
        self._clear_dirty()

    def _save_settings(self) -> None:
        APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
        settings = {
            "window_geometry": self.root.geometry(),
            "output_dir": self.output_dir_var.get().strip(),
            "package_name": self.package_var.get().strip(),
            "page_class": self.page_class_var.get().strip(),
            "steps_class": self.steps_class_var.get().strip(),
            "step_def_class": self.step_def_class_var.get().strip(),
            "page_url": self.generator_page_url_var.get().strip(),
            "queue_filter": self.queue_filter_var.get().strip(),
            "runner_timeout_ms": self.runner_timeout_var.get().strip(),
            "runner_step_delay_ms": self.runner_step_delay_var.get().strip(),
            "url_bar": self.url_var.get().strip(),
            "last_project_path": str(self.last_project_path) if self.last_project_path else "",
            "last_output_dir": str(self.last_output_dir) if self.last_output_dir else "",
            "last_runner_artifact_dir": str(self.last_runner_artifact_dir) if self.last_runner_artifact_dir else "",
        }
        SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    def _load_settings(self) -> None:
        APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not SETTINGS_PATH.exists():
            return
        try:
            settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        if settings.get("window_geometry"):
            try:
                self.root.geometry(settings["window_geometry"])
            except Exception:
                pass
        self.output_dir_var.set(settings.get("output_dir", ""))
        self.package_var.set(settings.get("package_name", "com.example.automation"))
        self.page_class_var.set(settings.get("page_class", "LoginPage"))
        self.steps_class_var.set(settings.get("steps_class", "LoginSteps"))
        self.step_def_class_var.set(settings.get("step_def_class", "LoginStepDefinitions"))
        self.generator_page_url_var.set(settings.get("page_url", ""))
        self.queue_filter_var.set(settings.get("queue_filter", ""))
        self.runner_timeout_var.set(settings.get("runner_timeout_ms", "10000"))
        self.runner_step_delay_var.set(settings.get("runner_step_delay_ms", "150"))
        self.url_var.set(settings.get("url_bar", self.url_var.get()))
        self.last_project_path = Path(settings["last_project_path"]) if settings.get("last_project_path") else None
        self.last_output_dir = Path(settings["last_output_dir"]) if settings.get("last_output_dir") else None
        self.last_runner_artifact_dir = Path(settings["last_runner_artifact_dir"]) if settings.get("last_runner_artifact_dir") else None

    def _load_last_project_if_available(self) -> None:
        candidate = self.last_project_path or (DEFAULT_PROJECT_PATH if DEFAULT_PROJECT_PATH.exists() else None)
        if candidate and candidate.exists():
            try:
                self._read_project_state(candidate)
                self.last_project_path = candidate
            except Exception:
                pass
        else:
            self._clear_dirty()

    def save_project_state(self) -> None:
        destination = filedialog.asksaveasfilename(
            title="Save Project State",
            defaultextension=".json",
            initialfile=(self.last_project_path.name if self.last_project_path else "save-page-desktop-project.json"),
            filetypes=[("JSON files", "*.json")],
        )
        if not destination:
            return
        self._write_project_state(Path(destination))
        self.last_project_path = Path(destination)
        self._save_settings()
        self._clear_dirty()
        self.messages.put(("info", f"Saved project state to {destination}"))

    def load_project_state(self) -> None:
        source = filedialog.askopenfilename(
            title="Load Project State",
            filetypes=[("JSON files", "*.json")],
        )
        if not source:
            return
        self._read_project_state(Path(source))
        self.last_project_path = Path(source)
        self._save_settings()
        self._clear_dirty()
        self.messages.put(("info", f"Loaded project state from {source}"))

    def open_output_folder(self) -> None:
        path = self.last_output_dir or (Path(self.output_dir_var.get()) if self.output_dir_var.get().strip() else None)
        if not path:
            self.messages.put(("error", "No generated output folder is available yet."))
            return
        try:
            open_path_in_shell(path)
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def open_runner_artifacts_folder(self) -> None:
        if not self.last_runner_artifact_dir:
            self.messages.put(("error", "No runner artifact folder is available yet."))
            return
        try:
            open_path_in_shell(self.last_runner_artifact_dir)
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def open_log_file(self) -> None:
        try:
            APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
            if not LOG_PATH.exists():
                LOG_PATH.touch()
            open_path_in_shell(LOG_PATH)
        except Exception as exc:
            self.messages.put(("error", str(exc)))

    def show_about(self) -> None:
        messagebox.showinfo(
            f"About {APP_NAME}",
            "\n".join(
                [
                    f"{APP_NAME} {APP_VERSION}",
                    "",
                    "Windows desktop utility for web capture, locator review,",
                    "Selenium/PageFactory code generation, and Gherkin playback.",
                    "",
                    f"Log file: {LOG_PATH}",
                ]
            ),
        )

    def _build_imported_element_details(self, node) -> Dict[str, Any]:
        attrs = dict(node.attrs)
        classes = attrs.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()
        text = " ".join(node.get_text(" ", strip=True).split())[:150]
        details = {
            "found": True,
            "count": 1,
            "tag": node.name.lower(),
            "id": attrs.get("id", ""),
            "name": attrs.get("name", ""),
            "dataTestId": attrs.get("data-testid", "") or attrs.get("data-test", ""),
            "ariaLabel": attrs.get("aria-label", ""),
            "placeholder": attrs.get("placeholder", ""),
            "type": attrs.get("type", ""),
            "text": text,
            "classes": classes[:4],
            "attrs": attrs,
            "xpath": "",
            "outerHtml": str(node)[:1500],
            "rect": {"x": 0, "y": 0, "width": 0, "height": 0},
        }
        return enrich_element_details(details)

    def _append_generator_element(self, candidate: Dict[str, Any]) -> None:
        candidate = dict(candidate)
        candidate["field_name"] = java_field_name(human_label(candidate))
        existing_names = {item["field_name"] for item in self.generator_elements}
        base_name = candidate["field_name"]
        suffix = 2
        while candidate["field_name"] in existing_names:
            candidate["field_name"] = f"{base_name}{suffix}"
            suffix += 1
        self.generator_elements.append(candidate)
        self._mark_dirty()

    def _handle_page_summary(self, future) -> None:
        try:
            summary = future.result()
        except Exception as exc:
            self.messages.put(("error", str(exc)))
            return
        text = json.dumps(summary, indent=2)
        self.root.after(0, lambda: self._set_text(self.catalog_output, text))
        self.messages.put(("info", "Loaded page summary."))

    def _handle_catalog_result(self, future) -> None:
        try:
            items = future.result()
        except Exception as exc:
            self.messages.put(("error", str(exc)))
            return
        lines = []
        for item in items:
            descriptor = f'{item["index"]:03d}. <{item["tag"]}>'
            extras = [item.get("id"), item.get("name"), item.get("dataTestId"), item.get("ariaLabel"), item.get("text")]
            lines.append(descriptor + " | " + " | ".join(filter(None, extras)))
        self.root.after(0, lambda: self._set_text(self.catalog_output, "\n".join(lines) or "No elements found."))
        self.messages.put(("info", "Loaded locator catalog."))

    def _handle_js_output(self, future) -> None:
        try:
            output = future.result()
        except Exception as exc:
            self.messages.put(("error", str(exc)))
            return
        self.root.after(0, lambda: self._set_text(self.js_output, output))
        self.messages.put(("info", "JavaScript executed."))

    def _handle_inspect_result(self, future) -> None:
        try:
            result = future.result()
        except Exception as exc:
            self.messages.put(("error", str(exc)))
            return

        self.root.after(0, lambda: self._apply_inspected_element(result))
        self.messages.put(("info", "Selector inspection complete."))

    def _handle_browser_picker_result(self, future) -> None:
        try:
            result = future.result()
        except Exception:
            return
        if not result:
            return
        self.root.after(0, lambda: self._apply_pending_capture(result))
        self.messages.put(("info", "Picked element received from browser. Review it in the app, then accept or discard."))

    def _handle_storage_result(self, future) -> None:
        try:
            counts = future.result()
        except Exception as exc:
            self.messages.put(("error", str(exc)))
            return
        message = f'Exported storage. localStorage={counts["localStorage"]}, sessionStorage={counts["sessionStorage"]}'
        self.messages.put(("info", message))

    def _handle_batch_result(self, future) -> None:
        try:
            files = future.result()
        except Exception as exc:
            self.batch_status_var.set("Batch: failed")
            self.messages.put(("error", str(exc)))
            return
        self.batch_status_var.set(f"Batch: completed ({len(files)} files)")
        self.messages.put(("info", f"Batch save completed. Saved {len(files)} pages."))

    def _handle_runner_result(self, future) -> None:
        try:
            result = future.result()
        except Exception as exc:
            self.runner_status_var.set("Runner: failed")
            self.messages.put(("error", f"Gherkin execution failed: {exc}"))
            return
        logs = result.get("logs", [])
        artifact_dir = result.get("artifact_dir", "")
        self.last_runner_artifact_dir = Path(artifact_dir) if artifact_dir else None
        self._save_settings()
        self.root.after(0, lambda: self._set_text(self.runner_log_output, "\n".join(logs)))
        if result.get("success"):
            self.runner_status_var.set(f"Runner: completed ({len(logs)} steps)")
            self.messages.put(("info", f"Gherkin execution completed. {len(logs)} steps passed. Report: {artifact_dir}"))
        else:
            self.runner_status_var.set("Runner: failed")
            failure_lines = logs + [f"FAIL - {result.get('error', 'Unknown error')}", f"Artifacts: {artifact_dir}"]
            self.root.after(0, lambda: self._set_text(self.runner_log_output, "\n".join(failure_lines)))
            self.messages.put(("error", f"Gherkin execution failed. Artifacts saved under {artifact_dir}"))

    def _handle_count_result(self, future, suffix: str) -> None:
        try:
            count = future.result()
        except Exception as exc:
            self.messages.put(("error", str(exc)))
            return
        self.messages.put(("info", f"{count} {suffix}."))

    def _set_text(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", text)

    def _apply_inspected_element(self, result: Dict[str, Any]) -> None:
        self.inspected_element = result
        inspect_text = json.dumps(
            {
                key: result[key]
                for key in ("tag", "count", "id", "name", "dataTestId", "ariaLabel", "placeholder", "type", "text", "xpath", "rect", "locators")
            },
            indent=2,
        )
        self._set_text(self.inspect_output, inspect_text)
        self._set_text(self.page_factory_output, result.get("page_factory", ""))

    def _apply_pending_capture(self, result: Dict[str, Any]) -> None:
        self.pending_capture_element = result
        label = human_label(result)
        tag = result.get("tag", "")
        locator = (result.get("locators") or [{}])[0]
        locator_text = f'{locator.get("type", "")}={locator.get("value", "")}'
        self.capture_review_var.set(f'Pending capture: {label} <{tag}> | {locator_text}')
        self._apply_inspected_element(result)

    def _refresh_generator_outputs(self) -> None:
        lines = []
        filter_text = self.queue_filter_var.get().strip().lower()
        for index, element in enumerate(self.generator_elements, start=1):
            locator = (element.get("locators") or [{}])[0]
            line = f'{index:02d}. {element["field_name"]} | {element.get("tag", "")} | {locator.get("type", "")}={locator.get("value", "")}'
            haystack = " ".join(
                [
                    element.get("field_name", ""),
                    element.get("tag", ""),
                    str(locator.get("type", "")),
                    str(locator.get("value", "")),
                    str(element.get("text", "")),
                    str(element.get("ariaLabel", "")),
                ]
            ).lower()
            if not filter_text or filter_text in haystack:
                lines.append(line)
        self._set_text(self.generator_list_output, "\n".join(lines) or "No queued elements.")
        if hasattr(self, "step_target_combo"):
            values = [element["field_name"] for element in self.generator_elements]
            self.step_target_combo["values"] = values
            if values and self.step_target_var.get() not in values:
                self.step_target_var.set(values[0])
            if not values:
                self.step_target_var.set("")
        self._set_text(self.custom_steps_output, "\n".join(self.custom_gherkin_steps) or "No custom scenario steps.")

        if not self.generator_elements:
            self._set_text(self.generator_output, "")
            self._set_text(self.gherkin_output, "")
            return

        artifacts = self._build_generator_artifacts(
            self.package_var.get().strip() or "com.example.automation",
            java_class_name(self.page_class_var.get().strip(), "GeneratedPage"),
            java_class_name(self.steps_class_var.get().strip(), "GeneratedSteps"),
            java_class_name(self.step_def_class_var.get().strip(), "GeneratedStepDefinitions"),
        )
        self._set_text(self.generator_output, artifacts["combined_preview"])
        self._set_text(self.gherkin_output, artifacts["gherkin"])

    def _build_generator_artifacts(self, package_name: str, page_class: str, steps_class: str, step_def_class: str) -> Dict[str, str]:
        page_package = f"{package_name}.pages"
        steps_package = f"{package_name}.steps"
        step_defs_package = f"{package_name}.stepdefinitions"
        page_lines = [
            f"package {page_package};",
            "",
            "import org.openqa.selenium.By;",
            "import org.openqa.selenium.WebDriver;",
            "import org.openqa.selenium.WebElement;",
            "import org.openqa.selenium.support.FindBy;",
            "import org.openqa.selenium.support.PageFactory;",
            "import org.openqa.selenium.support.ui.ExpectedConditions;",
            "import org.openqa.selenium.support.ui.Select;",
            "import org.openqa.selenium.support.ui.WebDriverWait;",
            "",
            "import java.time.Duration;",
            "",
            f"public class {page_class} {{",
            "    private final WebDriver driver;",
            "    private final WebDriverWait wait;",
            "",
        ]

        method_specs: List[Dict[str, str]] = []
        for element in self.generator_elements:
            preferred = (element.get("locators") or [{"type": "xpath", "value": element.get("xpath", "//body")}])[0]
            value = java_string_literal(preferred["value"])
            field_name = element["field_name"]
            action = infer_generator_action(element)

            annotation_map = {
                "id": f'    @FindBy(id = "{value}")',
                "name": f'    @FindBy(name = "{value}")',
                "css": f'    @FindBy(css = "{value}")',
                "xpath": f'    @FindBy(xpath = "{value}")',
            }
            by_map = {
                "id": f'By.id("{value}")',
                "name": f'By.name("{value}")',
                "css": f'By.cssSelector("{value}")',
                "xpath": f'By.xpath("{value}")',
            }

            page_lines.extend(
                [
                    annotation_map.get(preferred["type"], f'    @FindBy(xpath = "{value}")'),
                    f"    private WebElement {field_name};",
                    f"    private final By {field_name}By = {by_map.get(preferred['type'], f'By.xpath(\"{value}\")')};",
                    "",
                ]
            )
            method_specs.append(
                {
                    "field_name": field_name,
                    "action": action,
                    "readable": readable_field_name(field_name),
                    "tag": (element.get("tag") or "").lower(),
                }
            )

        page_lines.extend(
            [
                f"    public {page_class}(WebDriver driver) {{",
                "        this.driver = driver;",
                "        this.wait = new WebDriverWait(driver, Duration.ofSeconds(10));",
                "        PageFactory.initElements(driver, this);",
                "    }",
                "",
                "    public String getPageTitle() {",
                "        return driver.getTitle();",
                "    }",
                "",
            ]
        )

        for spec in method_specs:
            method_name = spec["field_name"][0].upper() + spec["field_name"][1:]
            if spec["action"] == "type":
                page_lines.extend(
                    [
                        f"    public void enter{method_name}(String value) {{",
                        f"        WebElement element = wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By));",
                        "        element.clear();",
                        "        element.sendKeys(value);",
                        "    }",
                        "",
                        f"    public String get{method_name}Value() {{",
                        f"        return wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By)).getAttribute(\"value\");",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "file":
                page_lines.extend(
                    [
                        f"    public void upload{method_name}(String filePath) {{",
                        f"        wait.until(ExpectedConditions.presenceOfElementLocated({spec['field_name']}By)).sendKeys(filePath);",
                        "    }",
                        "",
                        f"    public String get{method_name}Value() {{",
                        f"        return wait.until(ExpectedConditions.presenceOfElementLocated({spec['field_name']}By)).getAttribute(\"value\");",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "date":
                page_lines.extend(
                    [
                        f"    public void enter{method_name}Date(String value) {{",
                        f"        WebElement element = wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By));",
                        "        element.clear();",
                        "        element.sendKeys(value);",
                        "    }",
                        "",
                        f"    public String get{method_name}Value() {{",
                        f"        return wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By)).getAttribute(\"value\");",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "select":
                page_lines.extend(
                    [
                        f"    public void select{method_name}(String visibleText) {{",
                        f"        WebElement element = wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By));",
                        "        new Select(element).selectByVisibleText(visibleText);",
                        "    }",
                        "",
                        f"    public String getSelected{method_name}Option() {{",
                        f"        WebElement element = wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By));",
                        "        return new Select(element).getFirstSelectedOption().getText();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "checkbox":
                page_lines.extend(
                    [
                        f"    public void set{method_name}(boolean shouldBeChecked) {{",
                        f"        WebElement element = wait.until(ExpectedConditions.elementToBeClickable({spec['field_name']}By));",
                        "        if (element.isSelected() != shouldBeChecked) {",
                        "            element.click();",
                        "        }",
                        "    }",
                        "",
                        f"    public boolean is{method_name}Selected() {{",
                        f"        return wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By)).isSelected();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "radio":
                page_lines.extend(
                    [
                        f"    public void select{method_name}() {{",
                        f"        wait.until(ExpectedConditions.elementToBeClickable({spec['field_name']}By)).click();",
                        "    }",
                        "",
                        f"    public boolean is{method_name}Selected() {{",
                        f"        return wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By)).isSelected();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "table":
                page_lines.extend(
                    [
                        f"    public boolean does{method_name}ContainRowText(String expectedText) {{",
                        f"        WebElement table = wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By));",
                        "        for (WebElement row : table.findElements(By.tagName(\"tr\"))) {",
                        "            if (row.getText().contains(expectedText)) {",
                        "                return true;",
                        "            }",
                        "        }",
                        "        return false;",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "click":
                page_lines.extend(
                    [
                        f"    public void click{method_name}() {{",
                        f"        wait.until(ExpectedConditions.elementToBeClickable({spec['field_name']}By)).click();",
                        "    }",
                        "",
                    ]
                )
            page_lines.extend(
                [
                    f"    public boolean is{method_name}Displayed() {{",
                    f"        return wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By)).isDisplayed();",
                    "    }",
                    "",
                    f"    public String get{method_name}Text() {{",
                    f"        return wait.until(ExpectedConditions.visibilityOfElementLocated({spec['field_name']}By)).getText();",
                    "    }",
                    "",
                    f"    public boolean does{method_name}TextContain(String expectedText) {{",
                    f"        return get{method_name}Text().contains(expectedText);",
                    "    }",
                    "",
                ]
            )
        page_lines.append("}")

        steps_lines = [
            f"package {steps_package};",
            "",
            f'import {page_package}.{page_class};',
            "import org.openqa.selenium.WebDriver;",
            "",
            f"public class {steps_class} {{",
            f"    private final {page_class} page;",
            "",
            f"    public {steps_class}(WebDriver driver) {{",
            f"        this.page = new {page_class}(driver);",
            "    }",
            "",
            "    public String getPageTitle() {",
            "        return page.getPageTitle();",
            "    }",
            "",
        ]

        for spec in method_specs:
            method_name = spec["field_name"][0].upper() + spec["field_name"][1:]
            if spec["action"] == "type":
                steps_lines.extend(
                    [
                        f"    public void enter{method_name}(String value) {{",
                        f"        page.enter{method_name}(value);",
                        "    }",
                        "",
                        f"    public String get{method_name}Value() {{",
                        f"        return page.get{method_name}Value();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "file":
                steps_lines.extend(
                    [
                        f"    public void upload{method_name}(String filePath) {{",
                        f"        page.upload{method_name}(filePath);",
                        "    }",
                        "",
                        f"    public String get{method_name}Value() {{",
                        f"        return page.get{method_name}Value();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "date":
                steps_lines.extend(
                    [
                        f"    public void enter{method_name}Date(String value) {{",
                        f"        page.enter{method_name}Date(value);",
                        "    }",
                        "",
                        f"    public String get{method_name}Value() {{",
                        f"        return page.get{method_name}Value();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "select":
                steps_lines.extend(
                    [
                        f"    public void select{method_name}(String visibleText) {{",
                        f"        page.select{method_name}(visibleText);",
                        "    }",
                        "",
                        f"    public String getSelected{method_name}Option() {{",
                        f"        return page.getSelected{method_name}Option();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "table":
                steps_lines.extend(
                    [
                        f"    public boolean does{method_name}ContainRowText(String expectedText) {{",
                        f"        return page.does{method_name}ContainRowText(expectedText);",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "checkbox":
                steps_lines.extend(
                    [
                        f"    public void set{method_name}(boolean shouldBeChecked) {{",
                        f"        page.set{method_name}(shouldBeChecked);",
                        "    }",
                        "",
                        f"    public boolean is{method_name}Selected() {{",
                        f"        return page.is{method_name}Selected();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "radio":
                steps_lines.extend(
                    [
                        f"    public void select{method_name}() {{",
                        f"        page.select{method_name}();",
                        "    }",
                        "",
                        f"    public boolean is{method_name}Selected() {{",
                        f"        return page.is{method_name}Selected();",
                        "    }",
                        "",
                    ]
                )
            elif spec["action"] == "click":
                steps_lines.extend(
                    [
                        f"    public void click{method_name}() {{",
                        f"        page.click{method_name}();",
                        "    }",
                        "",
                    ]
                )
            steps_lines.extend(
                [
                    f"    public boolean is{method_name}Displayed() {{",
                    f"        return page.is{method_name}Displayed();",
                    "    }",
                    "",
                    f"    public String get{method_name}Text() {{",
                    f"        return page.get{method_name}Text();",
                    "    }",
                    "",
                    f"    public boolean does{method_name}TextContain(String expectedText) {{",
                    f"        return page.does{method_name}TextContain(expectedText);",
                    "    }",
                    "",
                ]
            )
        steps_lines.append("}")

        step_defs_lines = [
            f"package {step_defs_package};",
            "",
            f'import {steps_package}.{steps_class};',
            "import io.cucumber.java.en.And;",
            "import io.cucumber.java.en.Then;",
            "import io.cucumber.java.en.When;",
            "import org.junit.Assert;",
            "import org.openqa.selenium.WebDriver;",
            "",
            f"public class {step_def_class} {{",
            f"    private final {steps_class} steps;",
            "",
            f"    public {step_def_class}(TestContext context) {{",
            "        WebDriver driver = context.getDriver();",
            f"        this.steps = new {steps_class}(driver);",
            "    }",
            "",
        ]

        gherkin_lines = [
            f"Feature: {page_class} interactions",
            "",
            f"  Scenario: Use generated steps for {page_class}",
        ]
        if self.generator_page_url_var.get().strip():
            gherkin_lines.append(f'    Given I open "{self.generator_page_url_var.get().strip()}"')

        for spec in method_specs:
            method_name = spec["field_name"][0].upper() + spec["field_name"][1:]
            readable = spec["readable"]
            if spec["action"] == "type":
                step_defs_lines.extend(
                    [
                        f'    @When("I enter {{string}} into the {readable} field")',
                        f'    public void iEnterIntoThe{method_name}Field(String value) {{',
                        f"        steps.enter{method_name}(value);",
                        "    }",
                        "",
                        f'    @Then("the {readable} field value should be {{string}}")',
                        f'    public void the{method_name}FieldValueShouldBe(String expectedValue) {{',
                        f'        Assert.assertEquals(expectedValue, steps.get{method_name}Value());',
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f'    When I enter "sample value" into the {readable} field')
                gherkin_lines.append(f'    Then the {readable} field value should be "sample value"')
            elif spec["action"] == "file":
                step_defs_lines.extend(
                    [
                        f'    @When("I upload file {{string}} into the {readable} field")',
                        f'    public void iUploadFileIntoThe{method_name}Field(String filePath) {{',
                        f"        steps.upload{method_name}(filePath);",
                        "    }",
                        "",
                        f'    @Then("the {readable} field value should contain {{string}}")',
                        f'    public void the{method_name}FieldValueShouldContain(String expectedValuePart) {{',
                        f"        Assert.assertTrue(steps.get{method_name}Value().contains(expectedValuePart));",
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f'    When I upload file "C:\\\\path\\\\to\\\\file.txt" into the {readable} field')
                gherkin_lines.append(f'    Then the {readable} field value should contain "file.txt"')
            elif spec["action"] == "date":
                step_defs_lines.extend(
                    [
                        f'    @When("I enter date {{string}} into the {readable} field")',
                        f'    public void iEnterDateIntoThe{method_name}Field(String value) {{',
                        f"        steps.enter{method_name}Date(value);",
                        "    }",
                        "",
                        f'    @Then("the {readable} field value should be {{string}}")',
                        f'    public void the{method_name}DateFieldValueShouldBe(String expectedValue) {{',
                        f'        Assert.assertEquals(expectedValue, steps.get{method_name}Value());',
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f'    When I enter date "2026-05-17" into the {readable} field')
                gherkin_lines.append(f'    Then the {readable} field value should be "2026-05-17"')
            elif spec["action"] == "select":
                step_defs_lines.extend(
                    [
                        f'    @When("I select {{string}} from the {readable} dropdown")',
                        f'    public void iSelectFromThe{method_name}Dropdown(String option) {{',
                        f"        steps.select{method_name}(option);",
                        "    }",
                        "",
                        f'    @Then("the selected {readable} option should be {{string}}")',
                        f'    public void theSelected{method_name}OptionShouldBe(String expectedOption) {{',
                        f'        Assert.assertEquals(expectedOption, steps.getSelected{method_name}Option());',
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f'    When I select "sample option" from the {readable} dropdown')
                gherkin_lines.append(f'    Then the selected {readable} option should be "sample option"')
            elif spec["action"] == "checkbox":
                step_defs_lines.extend(
                    [
                        f'    @When("I set the {readable} checkbox to {{string}}")',
                        f'    public void iSetThe{method_name}CheckboxTo(String state) {{',
                        '        boolean shouldBeChecked = state.equalsIgnoreCase("checked") || state.equalsIgnoreCase("true");',
                        f"        steps.set{method_name}(shouldBeChecked);",
                        "    }",
                        "",
                        f'    @Then("the {readable} checkbox should be selected")',
                        f'    public void the{method_name}CheckboxShouldBeSelected() {{',
                        f"        Assert.assertTrue(steps.is{method_name}Selected());",
                        "    }",
                        "",
                        f'    @Then("the {readable} checkbox should not be selected")',
                        f'    public void the{method_name}CheckboxShouldNotBeSelected() {{',
                        f"        Assert.assertFalse(steps.is{method_name}Selected());",
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f'    When I set the {readable} checkbox to "checked"')
                gherkin_lines.append(f"    Then the {readable} checkbox should be selected")
            elif spec["action"] == "radio":
                step_defs_lines.extend(
                    [
                        f'    @When("I select the {readable} radio option")',
                        f'    public void iSelectThe{method_name}RadioOption() {{',
                        f"        steps.select{method_name}();",
                        "    }",
                        "",
                        f'    @Then("the {readable} radio option should be selected")',
                        f'    public void the{method_name}RadioOptionShouldBeSelected() {{',
                        f"        Assert.assertTrue(steps.is{method_name}Selected());",
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f"    When I select the {readable} radio option")
                gherkin_lines.append(f"    Then the {readable} radio option should be selected")
            elif spec["action"] == "click":
                step_defs_lines.extend(
                    [
                        f'    @When("I click the {readable} element")',
                        f'    public void iClickThe{method_name}Element() {{',
                        f"        steps.click{method_name}();",
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f"    When I click the {readable} element")
            if spec["action"] == "modal":
                step_defs_lines.extend(
                    [
                        f'    @Then("the {readable} modal should be visible")',
                        f'    public void the{method_name}ModalShouldBeVisible() {{',
                        f"        Assert.assertTrue(steps.is{method_name}Displayed());",
                        "    }",
                        "",
                        f'    @Then("the {readable} modal text should contain {{string}}")',
                        f'    public void the{method_name}ModalTextShouldContain(String expectedText) {{',
                        f"        Assert.assertTrue(steps.does{method_name}TextContain(expectedText));",
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f"    Then the {readable} modal should be visible")
                gherkin_lines.append(f'    Then the {readable} modal text should contain "sample text"')
            elif spec["action"] == "toast":
                step_defs_lines.extend(
                    [
                        f'    @Then("the {readable} toast should be visible")',
                        f'    public void the{method_name}ToastShouldBeVisible() {{',
                        f"        Assert.assertTrue(steps.is{method_name}Displayed());",
                        "    }",
                        "",
                        f'    @Then("the {readable} toast text should contain {{string}}")',
                        f'    public void the{method_name}ToastTextShouldContain(String expectedText) {{',
                        f"        Assert.assertTrue(steps.does{method_name}TextContain(expectedText));",
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f"    Then the {readable} toast should be visible")
                gherkin_lines.append(f'    Then the {readable} toast text should contain "sample text"')
            elif spec["action"] == "table":
                step_defs_lines.extend(
                    [
                        f'    @Then("the {readable} table should contain row text {{string}}")',
                        f'    public void the{method_name}TableShouldContainRowText(String expectedText) {{',
                        f"        Assert.assertTrue(steps.does{method_name}ContainRowText(expectedText));",
                        "    }",
                        "",
                    ]
                )
                gherkin_lines.append(f'    Then the {readable} table should contain row text "sample row text"')
            step_defs_lines.extend(
                [
                    f'    @Then("the {readable} element should be visible")',
                    f'    public void the{method_name}ElementShouldBeVisible() {{',
                    f"        Assert.assertTrue(steps.is{method_name}Displayed());",
                    "    }",
                    "",
                    f'    @Then("the {readable} element text should be {{string}}")',
                    f'    public void the{method_name}ElementTextShouldBe(String expectedText) {{',
                    f'        Assert.assertEquals(expectedText, steps.get{method_name}Text());',
                    "    }",
                    "",
                    f'    @Then("the {readable} element text should contain {{string}}")',
                    f'    public void the{method_name}ElementTextShouldContain(String expectedText) {{',
                    f'        Assert.assertTrue(steps.does{method_name}TextContain(expectedText));',
                    "    }",
                    "",
                ]
            )
            gherkin_lines.append(f"    Then the {readable} element should be visible")

        step_defs_lines.extend(
            [
                '    @Then("the page title should be {string}")',
                "    public void thePageTitleShouldBe(String expectedTitle) {",
                "        Assert.assertEquals(expectedTitle, steps.getPageTitle());",
                "    }",
                "}",
            ]
        )
        if self.custom_gherkin_steps:
            gherkin_lines.append("")
            gherkin_lines.append("    # Custom queued-element steps")
            gherkin_lines.extend(f"    {step}" if not step.startswith(("Given ", "When ", "Then ", "And ", "But ")) else f"    {step}" for step in self.custom_gherkin_steps)
        gherkin_lines.append('    Then the page title should be "expected title"')

        page_class_text = "\n".join(page_lines)
        steps_class_text = "\n".join(steps_lines)
        step_definitions_text = "\n".join(step_defs_lines)
        gherkin_text = "\n".join(gherkin_lines)
        combined_preview = "\n\n".join(
            [
                f"// {page_class}.java\n{page_class_text}",
                f"// {steps_class}.java\n{steps_class_text}",
                f"// {step_def_class}.java\n{step_definitions_text}",
                f"// {page_class}.feature\n{gherkin_text}",
            ]
        )
        return {
            "page_class": page_class_text,
            "steps_class": steps_class_text,
            "step_definitions_class": step_definitions_text,
            "gherkin": gherkin_text,
            "combined_preview": combined_preview,
        }

    def _pick_png(self, title: str) -> Optional[Path]:
        destination = filedialog.asksaveasfilename(
            title=title,
            defaultextension=".png",
            initialfile=self._default_filename(".png"),
            filetypes=[("PNG files", "*.png")],
        )
        return Path(destination) if destination else None

    def _pick_json(self, title: str, prefix: str) -> Optional[Path]:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        destination = filedialog.asksaveasfilename(
            title=title,
            defaultextension=".json",
            initialfile=f"{prefix}_{timestamp}.json",
            filetypes=[("JSON files", "*.json")],
        )
        return Path(destination) if destination else None

    def _default_filename(self, suffix: str) -> str:
        raw_title = self.title_var.get().replace("Title: ", "").strip() or "saved-page"
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return f"{sanitize_filename(raw_title)}_{timestamp}{suffix}"

    def _message_result(self, success_message: str):
        def callback(future) -> None:
            try:
                future.result()
            except Exception as exc:
                self.messages.put(("error", str(exc)))
                return
            self.messages.put(("info", success_message))
        return callback

    def _notify_result(self, success_message: str):
        return self._message_result(success_message)

    def _on_close(self) -> None:
        if self.is_dirty:
            choice = messagebox.askyesnocancel(
                APP_NAME,
                "You have unsaved project changes. Save them before closing?",
            )
            if choice is None:
                return
            if choice:
                try:
                    self._write_project_state(self.last_project_path or DEFAULT_PROJECT_PATH)
                    self.last_project_path = self.last_project_path or DEFAULT_PROJECT_PATH
                    self._clear_dirty()
                except Exception as exc:
                    messagebox.showerror(APP_NAME, f"Failed to save project state: {exc}")
                    return
        try:
            self._write_project_state(self.last_project_path or DEFAULT_PROJECT_PATH)
            self.last_project_path = self.last_project_path or DEFAULT_PROJECT_PATH
        except Exception:
            logging.exception("Failed to persist default project state on close.")
        try:
            self._save_settings()
        except Exception:
            logging.exception("Failed to save settings on close.")
        future = self.worker.run(self.worker.shutdown())
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self.root.destroy()


def main() -> None:
    mimetypes.init()
    APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOG_PATH),
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    root = Tk()
    root.geometry(DEFAULT_WINDOW_GEOMETRY)
    ttk.Style().theme_use("vista")
    SavePageApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
