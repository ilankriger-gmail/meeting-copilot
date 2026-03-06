# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Meeting Co-Pilot is a **single-file web application** (`meeting-copilot.html`) that provides real-time AI-assisted meeting support. It is built for a specific user (Ilan, a Brazilian influencer) and the entire UI is in **Brazilian Portuguese (pt-BR)**.

## How to Run

Open `meeting-copilot.html` directly in Chrome (required for Web Speech API support). No build step, no dependencies, no server needed.

## Architecture

Everything lives in one self-contained HTML file with three inline sections:

1. **CSS** (`<style>` block, lines 8–674) — Dark theme using CSS custom properties in `:root`. Design system uses `--accent` (#00ff88 green), `--accent2` (#0066ff blue), `--warn` (#ff6b35 orange), `--red` (#ff4444). Fonts: Syne (UI) and Space Mono (monospace elements).

2. **HTML** (lines 676–802) — Two-column layout:
   - **Left panel**: Live transcript with timestamped segments, interim speech indicators, and a key points bar
   - **Right panel**: Tabbed AI panel with Chat tab (conversational AI) and Insights tab (structured analysis cards)
   - **Header**: Meeting title input, timer, recording status, and action buttons
   - **Modal**: API key entry on first load

3. **JavaScript** (lines 804–1306) — All app logic, no modules or frameworks:
   - **State**: Global variables (`apiKey`, `recognition`, `isRecording`, `transcript[]`, `seconds`, etc.)
   - **Speech Recognition**: Uses `webkitSpeechRecognition` with `continuous: true` and `interimResults: true`, auto-restarts on end
   - **Anthropic API**: Direct browser calls to `https://api.anthropic.com/v1/messages` using `anthropic-dangerous-direct-browser-access` header. Uses model `claude-sonnet-4-20250514`
   - **Three AI call paths**: Chat (`callClaude`), Insights analysis (`runInsights` — expects JSON response), and Auto-analysis (`runAutoAnalysis` — fires every 90s during recording if transcript grew by 300+ chars)
   - **Export**: Downloads transcript as `.txt` file

## Key Technical Details

- The API key is stored only in a JS variable (`apiKey`), not persisted to localStorage
- Speech recognition language is hardcoded to `pt-BR`
- The insights analysis expects Claude to return a specific JSON structure: `{ insights: [{type, text}], keypoints: [string] }` where type is `blindspot`, `tip`, or `question`
- The system prompt in `callClaude()` includes the full transcript on every request (no conversation history is maintained)
- `max_tokens` is 600 for chat, 1200 for insights, 300 for auto-analysis
- There is an XSS risk: transcript text and API responses are inserted via `innerHTML` without sanitization
