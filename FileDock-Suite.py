import os
import sys
import json
import shutil
import hashlib
import subprocess
import traceback
import threading
from pathlib import Path
from datetime import datetime, timedelta
import customtkinter as ctk
from tkinter import ttk, messagebox, filedialog
from tkinter import simpledialog

# Matplotlib (embedding)
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

# Try to import tkinterdnd2 for drag-and-drop; fall back if not present
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except Exception:
    DND_AVAILABLE = False

# -------------------- App files & defaults --------------------
APP_DIR = Path(__file__).parent
CONFIG_FILE = APP_DIR / "config.json"
UNDO_FILE = APP_DIR / "undo_log.json"
HISTORY_FILE = APP_DIR / "history.json"
ERROR_LOG = APP_DIR / "error.log"

if not CONFIG_FILE.exists():
    messagebox.showerror("Missing config.json", f"Place config.json next to the script: {CONFIG_FILE}")
    raise SystemExit("config.json missing")

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    try:
        CATEGORIES = json.load(f)
    except Exception as e:
        messagebox.showerror("config.json error", f"Invalid JSON: {e}")
        raise SystemExit("invalid config.json")

# Ensure OTHERS exists if not present
if "OTHERS" not in CATEGORIES:
    CATEGORIES["OTHERS"] = []

# -------------------- Utilities --------------------
def log_error(exc: Exception):
    try:
        with ERROR_LOG.open("a", encoding="utf-8") as ef:
            ef.write(f"--- {datetime.now().isoformat()} ---\n")
            ef.write(traceback.format_exc())
            ef.write("\n\n")
    except Exception:
        pass

def find_category(ext: str) -> str:
    ext = ext.lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "OTHERS"

def safe_move(src: Path, dest: Path) -> Path:
    """Move src -> dest but if dest exists, add suffix _dupN. Returns Path of moved file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        src.rename(dest)
        return dest
    base = dest.stem
    suf = dest.suffix
    i = 1
    while True:
        candidate = dest.parent / f"{base}_dup{i}{suf}"
        if not candidate.exists():
            src.rename(candidate)
            return candidate
        i += 1

def save_undo(moves: list):
    """moves: list of dicts {'orig':orig_abs, 'new':new_abs}"""
    try:
        with UNDO_FILE.open("w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now().isoformat(), "moves": moves}, f, indent=2)
    except Exception as e:
        log_error(e)

def load_undo():
    if UNDO_FILE.exists():
        try:
            return json.loads(UNDO_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log_error(e)
    return None

def clear_undo():
    try:
        if UNDO_FILE.exists():
            UNDO_FILE.unlink()
    except Exception:
        pass

def save_history(folder: str):
    hist = []
    if HISTORY_FILE.exists():
        try:
            hist = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            hist = []
    if folder in hist:
        hist.remove(folder)
    hist.insert(0, folder)
    hist = hist[:6]
    try:
        HISTORY_FILE.write_text(json.dumps(hist, indent=2), encoding="utf-8")
    except Exception as e:
        log_error(e)

def load_history() -> list:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

# -------------------- File operations --------------------
def preview_counts(folder: Path) -> dict:
    counts = {cat: 0 for cat in CATEGORIES.keys()}
    counts.setdefault("OTHERS", 0)
    try:
        for item in folder.iterdir():
            if item.is_file():
                cat = find_category(item.suffix)
                counts[cat] = counts.get(cat, 0) + 1
    except Exception as e:
        log_error(e)
    return counts

def organize_folder(folder: Path, progress_callback=None):
    """Move files into category folders. Returns (moved_count, errors_list)."""
    moves = []
    errors = []
    try:
        items = [p for p in folder.iterdir() if p.is_file()]
        total = len(items)
        done = 0
        for p in items:
            try:
                cat = find_category(p.suffix)
                dest_folder = folder / cat
                dest_folder.mkdir(parents=True, exist_ok=True)
                dest = dest_folder / p.name
                moved_path = safe_move(p, dest)
                moves.append({"orig": str(p.resolve()), "new": str(moved_path.resolve())})
            except Exception as e:
                errors.append(f"Failed {p}: {e}")
                log_error(e)
            done += 1
            if progress_callback and total:
                try:
                    progress_callback(done, total)
                except Exception:
                    pass
    except Exception as e:
        errors.append(str(e))
        log_error(e)
    if moves:
        save_undo(moves)
    return len(moves), errors

def undo_last_operation():
    data = load_undo()
    if not data:
        return 0, "No undo data found."
    moves = data.get("moves", [])
    restored = 0
    errors = []
    # reverse moves
    for m in reversed(moves):
        try:
            orig = Path(m["orig"])
            new = Path(m["new"])
            if new.exists():
                # if orig already exists, create a safe restored name
                if orig.exists():
                    base = orig.stem
                    suf = orig.suffix
                    i = 1
                    candidate = orig.with_name(f"{base}_restored{i}{suf}")
                    while candidate.exists():
                        i += 1
                        candidate = orig.with_name(f"{base}_restored{i}{suf}")
                    new.rename(candidate)
                else:
                    new.rename(orig)
                restored += 1
        except Exception as e:
            errors.append(f"Failed to restore {m}: {e}")
            log_error(e)
    try:
        if UNDO_FILE.exists():
            UNDO_FILE.unlink()
    except Exception:
        pass
    msg = f"Restored {restored} files."
    if errors:
        msg += " Some errors occurred; check error.log."
    return restored, msg

# -------------------- Duplicates, large, recent --------------------
def md5_of_file(path: Path, block=65536):
    h = hashlib.md5()
    try:
        with path.open("rb") as f:
            buf = f.read(block)
            while buf:
                h.update(buf)
                buf = f.read(block)
        return h.hexdigest()
    except Exception as e:
        log_error(e)
        return None

def find_duplicates(folder: Path):
    map_hash = {}
    dup = {}
    try:
        for p in folder.iterdir():
            if p.is_file():
                h = md5_of_file(p)
                if not h:
                    continue
                if h in map_hash:
                    dup.setdefault(h, []).append(p)
                    # also add first occurrence if not already in list
                    if map_hash[h] not in dup[h]:
                        dup[h].insert(0, map_hash[h])
                else:
                    map_hash[h] = p
    except Exception as e:
        log_error(e)
    return dup  # {hash: [Path,...]}

def top_n_large(folder: Path, n=10):
    files = []
    try:
        for p in folder.iterdir():
            if p.is_file():
                try:
                    files.append((p, p.stat().st_size))
                except Exception:
                    pass
        files.sort(key=lambda x: x[1], reverse=True)
    except Exception as e:
        log_error(e)
    return files[:n]

def recent_files(folder: Path, days=7):
    cut = datetime.now().timestamp() - days*86400
    res = []
    try:
        for p in folder.iterdir():
            if p.is_file() and p.stat().st_mtime >= cut:
                res.append((p, p.stat().st_mtime))
        res.sort(key=lambda x: x[1], reverse=True)
    except Exception as e:
        log_error(e)
    return res

# -------------------- Function definitions (moved up) --------------------
def about_app():
    messagebox.showinfo(
        "About FileDock Suite",
        "FileDock Suite - Smart File Organizer\n"
        "Version: 1.0\n"
        "Author: Koushika Ram G\n\n"
        "Description:\n"
        "FileDock Suite helps you organize, analyze, and clean files easily.\n"
        "Features include duplicate detection, large file finder, and stats dashboard.\n\n"
        "Built with Python & CustomTkinter."
    )

def open_config():
    try:
        if sys.platform.startswith("win"):
            os.startfile(CONFIG_FILE)
        elif sys.platform.startswith("darwin"):
            subprocess.Popen(["open", str(CONFIG_FILE)])
        else:
            subprocess.Popen(["xdg-open", str(CONFIG_FILE)])
    except Exception as e:
        log_error(e)
        messagebox.showerror("Error", f"Cannot open config.json: {e}")

# -------------------- GUI tweaks & startup --------------------
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")

# If tkinterdnd2 available, use its Tk wrapper for drag-and-drop; else normal CTk()
if DND_AVAILABLE:
    root = TkinterDnD.Tk()
else:
    root = ctk.CTk()

root.title("FileDock Suite")
root.geometry("1150x720")
root.minsize(980, 620)

# global thread holder for duplicates
_dup_thread = None

# Variables
folder_var = ctk.StringVar()
search_var = ctk.StringVar()
history_vals = load_history()

# ---------------- Layout: Topbar, Sidebar, Content, Status ----------------
# Top bar with better button alignment
topbar = ctk.CTkFrame(root, height=60)
topbar.pack(side="top", fill="x")
topbar.pack_propagate(False)  # Maintain fixed height

# Left side - App title
app_title = ctk.CTkLabel(topbar, text="FileDock Suite", font=ctk.CTkFont(size=18, weight="bold"))
app_title.pack(side="left", padx=18, pady=15)  # Added pady for vertical centering

# Right side - Button container for better alignment
button_container = ctk.CTkFrame(topbar, fg_color="transparent")
button_container.pack(side="right", padx=14, pady=12)  # Added pady for vertical centering

# Buttons with consistent spacing
ctk.CTkButton(button_container, text="About", width=80, command=about_app).pack(side="right", padx=(0, 6))

def top_search_trigger(*_):
    do_preview()
search_var.trace_add("write", lambda *_: top_search_trigger())

# Sidebar and main content
main_area = ctk.CTkFrame(root)
main_area.pack(fill="both", expand=True)

sidebar = ctk.CTkFrame(main_area, width=260, corner_radius=8)
sidebar.grid(row=0, column=0, sticky="nsw", padx=12, pady=12)

content = ctk.CTkFrame(main_area, corner_radius=12)
content.grid(row=0, column=1, sticky="nsew", padx=(0,12), pady=12)
main_area.grid_columnconfigure(1, weight=1)
main_area.grid_rowconfigure(0, weight=1)

# Sidebar header + buttons (store refs for active highlighting)
title_label = ctk.CTkLabel(sidebar, text="FileDock Suite", font=ctk.CTkFont(size=16, weight="bold"))
title_label.pack(pady=(10,6))

sidebar_buttons = {}
def make_sidebar_btn(text, view):
    btn = ctk.CTkButton(sidebar, text=text, width=220, command=lambda v=view: show_view(v))
    btn.pack(pady=6)
    sidebar_buttons[view] = btn
    return btn

btn_org_widget = make_sidebar_btn("Organizer", "organizer")
btn_dash_widget = make_sidebar_btn("Dashboard", "dashboard")
btn_large_widget = make_sidebar_btn("Large Files", "large")
btn_recent_widget = make_sidebar_btn("Recent", "recent")
btn_dup_widget = make_sidebar_btn("Duplicates", "duplicates")

ctk.CTkButton(sidebar, text="Open config.json", width=220, command=open_config).pack(pady=(18,6))
theme_btn = ctk.CTkButton(sidebar, text="Toggle Dark Mode", width=220, command=lambda: toggle_theme())
theme_btn.pack(pady=6)

# History combobox
ctk.CTkLabel(sidebar, text="Recent folders:", anchor="w").pack(pady=(12,2), padx=8)
history_list = ttk.Combobox(sidebar, values=history_vals, width=30)
history_list.pack(padx=8)
def history_selected(e=None):
    v = history_list.get()
    if v:
        folder_var.set(v)
history_list.bind("<<ComboboxSelected>>", history_selected)

# Drag-and-Drop area
drop_label = ctk.CTkLabel(sidebar, text="(Drag a folder here)" if DND_AVAILABLE else "(Install tkinterdnd2 for drag-drop)")
drop_label.pack(pady=(12,10))
if DND_AVAILABLE:
    def handle_drop(event):
        data = event.data
        if data.startswith("{") and data.endswith("}"):
            data = data[1:-1]
        first = data.split()
        path = first[0]
        if os.path.isdir(path):
            folder_var.set(path)
            save_history(path)
            history_list['values'] = load_history()
        else:
            messagebox.showwarning("Drop error", "Please drop a folder (not files).")
    drop_label.drop_target_register(DND_FILES)
    drop_label.dnd_bind('<<Drop>>', handle_drop)

# ---- Content Frames (stacked) ----
views = {}
def clear_frame(frame):
    for w in frame.winfo_children():
        w.destroy()

# Organizer view
org_frame = ctk.CTkFrame(content, corner_radius=12)
views["organizer"] = org_frame

ctk.CTkLabel(org_frame, text="Select folder to organize:", anchor="w").pack(padx=12, pady=(12,6), fill="x")
entry = ctk.CTkEntry(org_frame, textvariable=folder_var, width=700)
entry.pack(padx=12)

def browse():
    f = filedialog.askdirectory()
    if f:
        folder_var.set(f)
        save_history(f)
        history_list['values'] = load_history()

ctk.CTkButton(org_frame, text="Browse", command=browse, width=140).pack(pady=10)

# Buttons row (use small buttons)
btn_row = ctk.CTkFrame(org_frame)
btn_row.pack(pady=6, padx=12, fill="x")
ctk.CTkButton(btn_row, text="Preview", command=lambda: do_preview(), width=100).pack(side="left", padx=6)
ctk.CTkButton(btn_row, text="Organize", command=lambda: do_organize(), fg_color="#00a86b", width=100).pack(side="left", padx=6)
ctk.CTkButton(btn_row, text="Undo Last", command=lambda: do_undo(), fg_color="#ff6b6b", width=100).pack(side="left", padx=6)
ctk.CTkButton(btn_row, text="Find Duplicates", command=lambda: show_view("duplicates"), width=120).pack(side="left", padx=6)
ctk.CTkButton(btn_row, text="Show Stats", command=lambda: show_view("dashboard"), width=120).pack(side="left", padx=6)

# Search & Tree preview
search_box = ctk.CTkEntry(org_frame, placeholder_text="Filter categories (type to filter)...", textvariable=search_var, width=520)
search_box.pack(padx=12, pady=(10,6))
search_var.trace_add("write", lambda *_: do_preview())

tree_card = ctk.CTkFrame(org_frame, corner_radius=8)
tree_card.pack(fill="both", expand=True, padx=12, pady=8)

cols = ("Category", "Count")
tree = ttk.Treeview(tree_card, columns=cols, show="headings", height=14)
tree.heading("Category", text="Category")
tree.heading("Count", text="Count")
tree.column("Category", width=520)
tree.column("Count", width=120, anchor="center")
tree.pack(fill="both", expand=True, side="left", padx=(0,6), pady=6)

# Scrollbar
sb = ttk.Scrollbar(tree_card, orient="vertical", command=tree.yview)
sb.pack(side="right", fill="y", pady=6)
tree.configure(yscrollcommand=sb.set)

# Progress and status (bottom of organizer)
progress = ctk.CTkProgressBar(org_frame)
progress.set(0)
progress.pack(fill="x", padx=12, pady=(4,6))
status_label = ctk.CTkLabel(org_frame, text="Ready", anchor="w")
status_label.pack(fill="x", padx=12, pady=(0,12))

# Dashboard view
dash_frame = ctk.CTkFrame(content, corner_radius=12)
views["dashboard"] = dash_frame

# Large files view
large_frame = ctk.CTkFrame(content, corner_radius=12)
views["large"] = large_frame

# Recent view
recent_frame = ctk.CTkFrame(content, corner_radius=12)
views["recent"] = recent_frame

# Duplicates view
dup_frame = ctk.CTkFrame(content, corner_radius=12)
views["duplicates"] = dup_frame

# Show/hide view
current_view = None
def set_active_sidebar(active_name):
    for name, btn in sidebar_buttons.items():
        if name == active_name:
            btn.configure(fg_color="#1f6aa5", text_color="white")  # active highlight
        else:
            btn.configure(fg_color="transparent", text_color=("gray10", "gray90"))  # reset

def show_view(name):
    global current_view
    if current_view:
        current_view.pack_forget()
    frame = views.get(name)
    frame.pack(fill="both", expand=True, padx=8, pady=8)
    current_view = frame
    set_active_sidebar(name)
    # auto-action when switched
    if name == "dashboard":
        render_dashboard()
    elif name == "large":
        render_large()
    elif name == "recent":
        render_recent()
    elif name == "duplicates":
        render_duplicates()
    elif name == "organizer":
        do_preview()

# ---------------- GUI actions (logic unchanged) ----------------
def do_preview():
    folder = folder_var.get().strip()
    if not folder:
        messagebox.showwarning("Select folder", "Choose a folder first.")
        return
    p = Path(folder)
    if not p.exists():
        messagebox.showerror("Invalid folder", "Folder doesn't exist.")
        return
    save_history(folder)
    counts = preview_counts(p)
    # update tree with alternating row tags
    for r in tree.get_children():
        tree.delete(r)
    i = 0
    for cat, cnt in counts.items():
        if search_var.get() and search_var.get().lower() not in cat.lower():
            continue
        tag = "even" if i % 2 == 0 else "odd"
        tree.insert("", "end", values=(cat, cnt), tags=(tag,))
        i += 1
    # tag styles via ttk.Style (works across themes)
    style = ttk.Style()
    try:
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 10))
        tree.tag_configure("even", background="#f7f8fa")
        tree.tag_configure("odd", background="#ffffff")
    except Exception:
        pass
    status_label.configure(text=f"Preview ready - {sum(counts.values())} files found.")
    root.update_idletasks()

def progress_callback(done, total):
    try:
        progress.set(done/total)
        root.update_idletasks()
    except Exception:
        pass

def do_organize():
    folder = folder_var.get().strip()
    if not folder:
        messagebox.showwarning("Select folder", "Choose a folder first.")
        return
    p = Path(folder)
    if not p.exists():
        messagebox.showerror("Invalid folder", "Folder doesn't exist.")
        return
    if not messagebox.askyesno("Confirm", f"Organize files inside:\n{folder}\nThis will MOVE files into category folders. Continue?"):
        return
    progress.set(0)
    status_label.configure(text="Organizing...")
    root.update_idletasks()
    moved, errors = organize_folder(p, progress_callback=progress_callback)
    status = f"Moved {moved} files."
    if errors:
        status += f" {len(errors)} errors (see error.log)."
    status_label.configure(text=status)
    messagebox.showinfo("Done", status)
    do_preview()

def do_undo():
    restored, msg = undo_last_operation()
    messagebox.showinfo("Undo", msg)
    do_preview()

# -------------- Render subviews (UX improved; logic preserved) --------------
def render_dashboard():
    clear_frame(dash_frame := views["dashboard"])
    ctk.CTkButton(dash_frame, text="← Back", width=80, command=lambda: show_view("organizer")).pack(anchor="w", padx=12, pady=8)
    folder = folder_var.get().strip()
    if not folder or not Path(folder).exists():
        ctk.CTkLabel(dash_frame, text="Select a folder and click Preview to build stats.", font=ctk.CTkFont(size=12)).pack(pady=20)
        return
    counts = preview_counts(Path(folder))
    labels = [k for k,v in counts.items() if v>0]
    sizes = [v for v in counts.values() if v>0]
    if not sizes:
        ctk.CTkLabel(dash_frame, text="No files to show in stats.", font=ctk.CTkFont(size=12)).pack(pady=20)
        return
    fig, ax = plt.subplots(figsize=(6,4))
    ax.pie(sizes, labels=labels, autopct="%1.1f%%")
    ax.set_title("File distribution")
    # Respect theme background
    canvas = FigureCanvasTkAgg(fig, master=dash_frame)
    canvas.draw()
    canvas.get_tk_widget().pack(fill="both", expand=True, padx=10, pady=6)
    toolbar = NavigationToolbar2Tk(canvas, dash_frame)
    toolbar.update()
    toolbar.pack()
    plt.close(fig)  # Prevent memory leaks

def render_large():
    clear_frame(large_frame := views["large"])
    ctk.CTkButton(large_frame, text="← Back", width=80, command=lambda: show_view("organizer")).pack(anchor="w", padx=12, pady=8)
    folder = folder_var.get().strip()
    if not folder or not Path(folder).exists():
        ctk.CTkLabel(large_frame, text="Select a folder and click Preview.", font=ctk.CTkFont(size=12)).pack(pady=20)
        return
    top = top_n_large(Path(folder), 10)
    ctk.CTkLabel(large_frame, text="Top large files (Top 10):", font=ctk.CTkFont(size=13)).pack(pady=8)
    tv = ttk.Treeview(large_frame, columns=("File","SizeMB"), show="headings", height=12)
    tv.heading("File", text="File")
    tv.heading("SizeMB", text="Size (MB)")
    tv.column("File", width=700)
    tv.column("SizeMB", width=120, anchor="center")
    tv.pack(fill="both", expand=True, padx=8, pady=6)
    for p, s in top:
        tv.insert("", "end", values=(str(p), f"{s/1024/1024:.2f}"))

def render_recent():
    clear_frame(recent_frame := views["recent"])
    ctk.CTkButton(recent_frame, text="← Back", width=80, command=lambda: show_view("organizer")).pack(anchor="w", padx=12, pady=8)
    folder = folder_var.get().strip()
    if not folder or not Path(folder).exists():
        ctk.CTkLabel(recent_frame, text="Select a folder and click Preview.", font=ctk.CTkFont(size=12)).pack(pady=20)
        return
    rec = recent_files(Path(folder), days=7)
    ctk.CTkLabel(recent_frame, text="Recent files (last 7 days):", font=ctk.CTkFont(size=13)).pack(pady=8)
    tv = ttk.Treeview(recent_frame, columns=("File","Modified"), show="headings", height=12)
    tv.heading("File", text="File")
    tv.heading("Modified", text="Modified")
    tv.column("File", width=700)
    tv.column("Modified", width=180, anchor="center")
    tv.pack(fill="both", expand=True, padx=8, pady=6)
    for p, ts in rec:
        tv.insert("", "end", values=(str(p), datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")))

def render_duplicates():
    """
    Render the duplicates view and run find_duplicates in a background thread.
    """
    clear_frame(dup_frame := views["duplicates"])
    ctk.CTkButton(dup_frame, text="← Back", width=80, command=lambda: show_view("organizer")).pack(anchor="w", padx=12, pady=8)

    folder = folder_var.get().strip()
    if not folder or not Path(folder).exists():
        ctk.CTkLabel(dup_frame, text="Select a folder and click Preview.", font=ctk.CTkFont(size=12)).pack(pady=20)
        return

    # Status label and tree placeholder
    status = ctk.CTkLabel(dup_frame, text="Scanning for duplicates...", anchor="w")
    status.pack(fill="x", padx=12, pady=(4,8))

    tv = ttk.Treeview(dup_frame, columns=("Hash","File"), show="headings", height=14)
    tv.heading("Hash", text="Hash")
    tv.heading("File", text="File path")
    tv.column("Hash", width=260)
    tv.column("File", width=860)
    tv.pack(fill="both", expand=True, padx=12, pady=6)

    delete_btn = ctk.CTkButton(dup_frame, text="Delete Selected", command=lambda: None)
    delete_btn.pack(pady=8)
    delete_btn.configure(state="disabled")

    # worker to run in background
    def worker(folder_path):
        try:
            result = find_duplicates(Path(folder_path))
        except Exception as e:
            result = {}
            log_error(e)

        # callback to populate UI from main thread
        def on_done():
            for h, paths in result.items():
                for p in paths:
                    tv.insert("", "end", values=(h, str(p)))
            if tv.get_children():
                delete_btn.configure(state="normal")
                status.configure(text=f"Scan complete - {len(tv.get_children())} file entries.")
            else:
                status.configure(text="No duplicates found.")
                delete_btn.configure(state="disabled")

            def delete_selected():
                sel = tv.selection()
                if not sel:
                    messagebox.showwarning("Select", "No rows selected.")
                    return
                if not messagebox.askyesno("Confirm delete", "Delete selected files permanently? This cannot be undone."):
                    return
                deleted = 0
                for s in sel:
                    vals = tv.item(s, "values")
                    try:
                        Path(vals[1]).unlink()
                        deleted += 1
                    except Exception as e:
                        log_error(e)
                messagebox.showinfo("Deleted", f"Deleted {deleted} files.")
                render_duplicates()

            delete_btn.configure(command=delete_selected)

        root.after(0, on_done)

    # Start thread
    global _dup_thread
    _dup_thread = threading.Thread(target=worker, args=(folder,), daemon=True)
    _dup_thread.start()

# ---------------- theme toggle ----------------
def toggle_theme():
    cur = ctk.get_appearance_mode()
    ctk.set_appearance_mode("Dark" if cur != "Dark" else "Light")
    # adjust title color for visibility
    cur2 = ctk.get_appearance_mode()
    if cur2 == "Dark":
        title_label.configure(fg_color=None)
        app_title.configure(text_color="white")
    else:
        app_title.configure(text_color="black")

# ---------------- initial view & start ----------------
show_view("organizer")
history_list['values'] = load_history()

if __name__ == "__main__":
    try:
        root.mainloop()
    except Exception as e:
        log_error(e)
        messagebox.showerror("Fatal error", f"An unexpected error occurred. See error.log.\n\n{e}")