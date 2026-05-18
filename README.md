# Web Capture Automation Studio

Windows desktop app built with Python plus PowerShell helpers.

It launches a controlled Chromium browser window and saves the page currently displayed as a single `.html` file with resources inlined as data URLs where possible.
It also includes Selenium-oriented utilities for Page Object Model and Page Factory work.

## What it does

- Open a Chromium window for browsing
- Track the current page URL and title
- Save the current rendered page into one HTML file
- Save viewport screenshots
- Save full-page screenshots
- Inspect a CSS selector and show element details
- Highlight matching elements in the page
- Enable a browser-side picker button and click elements directly in Chromium
- Wait for a selector to become visible
- Generate Java Selenium PageFactory snippets
- Queue inspected elements for bulk generation
- Import a saved HTML page and queue locators from it
- Preserve the original page URL in saved HTML and prepend navigation to generated Gherkin
- Ask for output folder, page class, steps class, and step definitions class
- Rename, delete, reorder, and filter queued generator elements
- Generate organized Java `pages`, `steps`, and `stepdefinitions` files
- Generate matching `.feature` Gherkin steps
- Add custom queued-element Gherkin steps from the code generator UI and refresh preview automatically
- Load generated or saved Gherkin into a scenario runner tab
- Execute supported generated Gherkin steps against the live Chromium page
- Control runner wait time and inter-step delay for more reliable playback
- Generate and execute file upload steps
- Generate and execute date input steps
- Generate and execute modal/dialog visibility and text assertions
- Generate and execute toast/snackbar visibility and text assertions
- Generate and execute table row text assertions
- Support setup `Given` steps such as opening URLs, clearing/restoring cookies and storage, and resizing the browser
- Support frame and window switching in the scenario runner
- Support hover, keyboard actions, and drag-and-drop in the scenario runner
- Support richer waits such as URL checks, text-appear checks, disappear checks, and idle waits
- Save runner failure artifacts including screenshot, HTML, and execution log
- Save and load full project workspace JSON state
- Persist settings such as output folder, class names, waits, page URL, and last-used project
- Warn about unsaved project changes before closing
- Write an application log file under `.save-page-desktop\app.log`
- Open generated output and runner artifact folders directly from the app
- Show built-in Help guidance and keyboard shortcuts
- Export cookies as JSON
- Export localStorage and sessionStorage as JSON
- Run custom JavaScript against the current page
- Generate a quick locator catalog for common controls
- Batch-open and save a list of URLs
- Preserve common live state such as form values and checked inputs
- Inline common external resources:
  - stylesheets
  - scripts
  - images
  - favicons
  - media/poster URLs referenced by HTML and CSS

## Limits

No tool can guarantee perfect capture for every website. The hardest cases are:

- DRM or protected media
- pages that render inside browser extensions or PDFs
- resources generated only inside closed shadow DOM
- pages that depend on service workers or live server APIs after reopening
- cross-origin assets that were never loaded during browsing

This app is designed to work well for a large class of normal websites, especially pages that have already fully loaded in the controlled browser.

## Setup

Run in PowerShell:

```powershell
.\install.ps1
.\run.ps1
```

To build a Windows executable:

```powershell
.\build.ps1
```

That produces the branded `WebCaptureAutomationStudio` executable folder in `.\dist\WebCaptureAutomationStudio` and uses the packaged Windows icon from `assets\web-capture-automation-studio.ico`.
The packaged app prefers installed Microsoft Edge or Google Chrome on Windows, so it does not rely on a bundled Playwright browser being present inside the executable folder.

## Usage

1. Click `Launch Browser`.
2. Browse in the Chromium window.
3. Use the app tabs depending on your task:
   - `Save & Capture` for HTML save, screenshots, cookies, and storage
   - `Locators & POM` for selector inspection, browser-side picking, highlighting, and PageFactory snippet generation
   - `Automation` for wait checks, JavaScript execution, and page catalogs
   - `Batch` for multi-URL save runs
   - `Code Generator` to choose the output folder and class names, import saved HTML, add custom queued-element steps, and generate page/steps/step-definitions/Gherkin files
   - `Scenario Runner` to paste or load generated Gherkin and execute it against the current browser page
   - `Help` for supported flows, runner coverage, and packaging reminders
4. For Selenium POM work, either inspect a selector or use the browser picker in `Locators & POM`, add elements to the generator list, or import a saved HTML page in `Code Generator`.
5. In `Code Generator`, the imported or current page URL becomes the first generated navigation step, and any custom queued-element steps you add are appended into the Gherkin automatically.
6. Save the current workspace with `Save Project` if you want to reopen the same queue, custom steps, and runner scenario later.
7. To execute the generated behavior, load the generated Gherkin into `Scenario Runner`, keep the matching locator queue in place, then run the scenario with your preferred wait and delay values.

## Shortcuts

- `Ctrl+S` save project state
- `Ctrl+O` load project state
- `Ctrl+G` generate Java and Gherkin files
- `F5` run the scenario runner

## Files

- `install.ps1` creates the virtual environment, installs Python packages, and installs Chromium for Playwright.
- `run.ps1` starts the desktop app.
- `src/main.py` is the application entry point.
