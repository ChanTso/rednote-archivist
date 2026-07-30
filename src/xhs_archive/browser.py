from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

from .config import AppConfig

XHS_HOME = "https://www.xiaohongshu.com/"


def chrome_executable_path() -> str | None:
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def human_delay(cfg: AppConfig) -> None:
    time.sleep(random.uniform(cfg.browser.min_delay_seconds, cfg.browser.max_delay_seconds))


def _launch_kwargs(cfg: AppConfig, headed: bool | None = None) -> dict:
    kwargs: dict = {
        "user_data_dir": str(cfg.browser_profile_dir),
        "headless": cfg.browser.headless if headed is None else not headed,
        "slow_mo": cfg.browser.slow_mo_ms,
        "viewport": {"width": 1440, "height": 1100},
        "accept_downloads": True,
    }
    executable = chrome_executable_path()
    if executable:
        kwargs["executable_path"] = executable
    return kwargs


def launch_persistent_chromium(playwright, cfg: AppConfig, headed: bool | None = None):
    kwargs = _launch_kwargs(cfg, headed=headed)
    user_data_dir = kwargs.pop("user_data_dir")
    return playwright.chromium.launch_persistent_context(user_data_dir, **kwargs)


def is_auth_required_text(text: str) -> bool:
    markers = [
        "请先登录",
        "扫码登录",
        "手机号登录",
        "输入验证码",
        "安全验证",
        "访问异常",
        "操作频繁",
        "当前行为存在风险",
        "请完成验证",
    ]
    return any(marker in text for marker in markers)


def detect_logged_in(page) -> bool:
    text = ""
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False
    if "登录" in text and ("验证码" in text or "扫码" in text or "手机号" in text):
        return False
    positive_markers = ["创作中心", "消息", "收藏", "个人", "首页"]
    return sum(marker in text for marker in positive_markers) >= 2


def login(cfg: AppConfig, *, timeout_seconds: int = 900) -> dict:
    from playwright.sync_api import sync_playwright

    cfg.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = launch_persistent_chromium(p, cfg, headed=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(XHS_HOME, wait_until="domcontentloaded", timeout=60_000)
        print(
            "请在打开的浏览器中完成小红书登录/验证；检测到登录成功后会自动继续。",
            file=sys.stderr,
            flush=True,
        )
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                page.wait_for_timeout(3000)
                if detect_logged_in(page):
                    storage = context.storage_state()
                    context.close()
                    return {"ok": True, "storage_origins": len(storage.get("origins", []))}
            except KeyboardInterrupt:
                context.close()
                raise
            except Exception:
                pass
        screenshot = cfg.logs_dir / "auth_timeout.png"
        try:
            page.screenshot(path=str(screenshot), full_page=True)
        except Exception:
            pass
        context.close()
        return {"ok": False, "reason": "login_timeout", "screenshot": str(screenshot)}


def collect_note_links_from_page(page) -> list[dict[str, str]]:
    script = """
    () => Array.from(document.querySelectorAll('a[href]'))
      .map(a => {
        const rect = a.getBoundingClientRect();
        const href = a.href;
        const visible = rect.width > 10 && rect.height > 10;
        const score =
          (visible ? 100 : 0) +
          (href.includes('/board/') ? 50 : 0) +
          (href.includes('xsec_token=') ? 30 : 0) +
          (href.includes('/explore/') ? 10 : 0);
        return { href, text: (a.innerText || a.textContent || '').trim(), score };
      })
      .filter(x => /xiaohongshu\\.com\\/(explore|discovery\\/item|board)\\//.test(x.href))
      .sort((a, b) => b.score - a.score)
    """
    return page.evaluate(script)


def collect_image_urls_from_page(page) -> list[str]:
    script = """
    () => {
      const regions = Array.from(document.querySelectorAll('article, [role="article"], [role="dialog"], main, .swiper, .slider'));
      const scope = regions.length ? regions : [document.body];
      const urls = [];
      for (const region of scope) {
        for (const img of region.querySelectorAll('img')) {
          const rect = img.getBoundingClientRect();
          const src = img.currentSrc || img.src || img.getAttribute('data-src') || '';
          if (!src) continue;
          if (rect.width < 80 || rect.height < 80) continue;
          if (/avatar|icon|logo|emoji/i.test(src)) continue;
          urls.push(src);
        }
      }
      return Array.from(new Set(urls));
    }
    """
    return page.evaluate(script)


def collect_image_candidates_from_page(page) -> list[dict]:
    script = """
    () => {
      const parseSrcset = (value) => {
        if (!value) return [];
        return value.split(',').map(part => {
          const bits = part.trim().split(/\\s+/);
          const url = bits[0];
          let score = 0;
          for (const bit of bits.slice(1)) {
            if (bit.endsWith('w')) score = Math.max(score, parseInt(bit, 10) || 0);
            if (bit.endsWith('x')) score = Math.max(score, Math.round((parseFloat(bit) || 0) * 1000));
          }
          return {url, score};
        }).filter(item => item.url);
      };
      const regions = Array.from(document.querySelectorAll('article, [role="article"], [role="dialog"], main, .swiper, .slider'));
      const scope = regions.length ? regions : [document.body];
      const groups = [];
      for (const region of scope) {
        for (const img of region.querySelectorAll('img')) {
          const rect = img.getBoundingClientRect();
          if (rect.width < 80 || rect.height < 80) continue;
          const current = img.currentSrc || img.src || '';
          if (/avatar|icon|logo|emoji/i.test(current)) continue;
          const candidates = [];
          const original = img.getAttribute('data-original') || img.getAttribute('data-origin') || img.getAttribute('data-src');
          if (original) candidates.push({url: original, source_variant: 'data-original', score: 80});
          for (const item of parseSrcset(img.getAttribute('srcset'))) {
            candidates.push({url: item.url, source_variant: 'largest-srcset', score: 70 + Math.min(item.score, 10000)});
          }
          if (current) candidates.push({url: current, source_variant: 'current-src', score: 20});
          if (!candidates.length) continue;
          groups.push({
            display_width: Math.round(rect.width),
            display_height: Math.round(rect.height),
            candidates
          });
        }
      }
      return groups;
    }
    """
    return page.evaluate(script)


def note_state_from_page(page, note_id: str) -> dict | None:
    script = """
    (noteId) => {
      const state = window.__INITIAL_STATE__;
      const note = state && state.note && state.note.noteDetailMap && state.note.noteDetailMap[noteId] && state.note.noteDetailMap[noteId].note;
      return note || null;
    }
    """
    try:
        return page.evaluate(script, note_id)
    except Exception:
        return None


def request_fetcher_from_context(context):
    def fetch(url: str) -> bytes:
        response = context.request.get(url, timeout=60_000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status}: {url}")
        return response.body()

    return fetch


def dump_browser_state(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
