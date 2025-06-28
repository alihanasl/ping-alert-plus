

import os
import sys
import json
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import subprocess
import requests
import smtplib
import re
import csv
from email.message import EmailMessage
from win10toast_click import ToastNotifier
from pystray import Icon as TrayIcon, Menu as TrayMenu, MenuItem as item
from PIL import Image, ImageDraw

# --- Setup and Constants ---
def get_app_dir():
    return os.path.dirname(sys.executable) if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))

APP_DIR = get_app_dir()
LISTS_DIR = os.path.join(APP_DIR, "device_lists")
LOG_FILE = os.path.join(APP_DIR, "unreachable_log.txt")
LATENCY_LOG_FILE = os.path.join(APP_DIR, "latency_log.csv")
CONFIG_FILE = os.path.join(APP_DIR, "telegram_config.json")
EMAIL_CONFIG_FILE = os.path.join(APP_DIR, "email_config.json")

os.makedirs(LISTS_DIR, exist_ok=True)

YELLOW_TO_GREEN_THRESHOLD = 100
COLOR_GREEN, COLOR_YELLOW, COLOR_RED, COLOR_DEFAULT = '#90EE90', '#FFFFE0', '#FFB6C1', 'white'

# --- Globals ---
toaster = ToastNotifier()
tray_icon = None
monitoring_thread = None
monitoring_active = False
devices = []
main_root = None

# --- State Management ---
device_states = {}

# --- Core Logic Functions ---
def log_event(file, message):
    with open(file, "a", encoding="utf-8-sig") as f:
        f.write(message)

def log_latency(name, ip, latency):
    file_exists = os.path.isfile(LATENCY_LOG_FILE)
    with open(LATENCY_LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["Timestamp", "Device Name", "IP Address", "Latency (ms)"])
        writer.writerow([time.strftime('%d.%m.%Y %H:%M:%S'), name, ip, latency])

def send_notification(subject, body):
    show_toast(subject, body)
    send_telegram_message(f"{subject}: {body}")
    send_email_alert(subject, body)

def show_toast(title, message):
    try:
        toaster.show_toast(title, message, duration=5, threaded=True)
    except Exception as e:
        print(f"Toast error: {e}")

def send_telegram_message(message):
    if not os.path.exists(CONFIG_FILE): return
    try:
        with open(CONFIG_FILE, "r") as f: config = json.load(f)
        bot_token, chat_id = config.get("bot_token"), config.get("chat_id")
        if bot_token and chat_id:
            requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", data={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        print(f"Telegram message failed: {e}")

def send_email_alert(subject, body):
    if not os.path.exists(EMAIL_CONFIG_FILE): return
    try:
        with open(EMAIL_CONFIG_FILE, "r") as f: config = json.load(f)
        msg = EmailMessage()
        msg.set_content(body)
        msg["Subject"], msg["From"], msg["To"] = subject, config["email"], config["receiver"]
        with smtplib.SMTP(config["smtp_server"], int(config["smtp_port"])) as server:
            server.starttls()
            server.login(config["email"], config["password"])
            server.send_message(msg)
    except Exception as e:
        print(f"Email alert failed: {e}")

def ping(ip):
    try:
        CREATE_NO_WINDOW = 0x08000000
        command = ["ping", "-n", "1", "-w", "2000", ip] if os.name == "nt" else ["ping", "-c", "1", "-W", "2", ip]
        result = subprocess.run(command, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=3)
        if result.returncode == 0:
            match = re.search(r"time[=<]([\d.]+)ms", result.stdout)
            return int(float(match.group(1))) if match else 1
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

def ping_worker(device, listbox, idx):
    ip, name = device['ip'], device['name']
    state = device_states.setdefault(ip, {'status': 'NEUTRAL', 'success_streak': 0, 'last_alert': 0})
    
    latency = ping(ip)
    
    current_status = state['status']
    new_status = current_status

    if latency is not None:
        log_latency(name, ip, latency)
        state['success_streak'] += 1
        if current_status == 'NEUTRAL':
            new_status = 'GREEN'
        elif current_status == 'RED':
            new_status = 'YELLOW'
            state['success_streak'] = 1
            send_notification(f"✅ Device Back Online: {name}", f"{name} ({ip}) is now reachable.")
        elif current_status == 'YELLOW' and state['success_streak'] >= YELLOW_TO_GREEN_THRESHOLD:
            new_status = 'GREEN'
    else: # Ping failed
        state['success_streak'] = 0
        if current_status != 'RED':
            new_status = 'RED'
            now = time.time()
            if now - state.get('last_alert', 0) >= 1800:
                log_event(LOG_FILE, f"{ip} - {name} (Unreachable on {time.strftime('%d.%m.%Y at %H:%M')})\n")
                send_notification(f"switch-alert: {name} is down", f"{name} ({ip}) is unreachable!")
                state['last_alert'] = now
    
    state['status'] = new_status
    color = {'NEUTRAL': COLOR_DEFAULT, 'GREEN': COLOR_GREEN, 'YELLOW': COLOR_YELLOW, 'RED': COLOR_RED}[new_status]
    main_root.after(0, lambda: listbox.itemconfig(idx, {'bg': color}))

def monitor_master_loop(listbox, start_btn, stop_btn):
    global monitoring_active
    monitoring_active = True

    while monitoring_active:
        threads = [threading.Thread(target=ping_worker, args=(dev, listbox, i), daemon=True) for i, dev in enumerate(devices)]
        for t in threads: t.start()
        time.sleep(5) # Interval between full cycles

    if main_root:
        main_root.after(0, lambda: (start_btn.config(state="normal"), stop_btn.config(state="disabled")))
        for i in range(listbox.size()): main_root.after(0, lambda i=i: listbox.itemconfig(i, {'bg': COLOR_DEFAULT}))

def start_monitoring(listbox, start_btn, stop_btn):
    start_btn.config(state="disabled")
    stop_btn.config(state="normal")
    global device_states
    device_states.clear()
    for i, dev in enumerate(devices):
        device_states[dev['ip']] = {'status': 'NEUTRAL', 'success_streak': 0, 'last_alert': 0}
        listbox.itemconfig(i, {'bg': COLOR_DEFAULT})

    global monitoring_thread
    monitoring_thread = threading.Thread(target=lambda: monitor_master_loop(listbox, start_btn, stop_btn), daemon=True)
    monitoring_thread.start()

def stop_monitoring():
    global monitoring_active
    monitoring_active = False

# --- GUI Functions ---
def on_closing():
    if messagebox.askokcancel("Quit", "Quit Ping Alert+?"):
        stop_monitoring()
        if tray_icon and tray_icon.visible: tray_icon.stop()
        main_root.destroy()
        os._exit(0)

def hide_to_tray():
    main_root.withdraw()
    image = Image.new("RGB", (64, 64), "black"); ImageDraw.Draw(image).ellipse((4, 4, 60, 60), fill="green")
    menu = TrayMenu(item('Show', show_window, default=True), item('Exit', on_exit_tray))
    global tray_icon
    tray_icon = TrayIcon("PingAlert+", image, "Ping Alert+", menu)
    tray_icon.run()

def show_window():
    if tray_icon: tray_icon.stop()
    main_root.after(0, main_root.deiconify)

def on_exit_tray():
    stop_monitoring()
    if tray_icon: tray_icon.stop()
    main_root.destroy()
    os._exit(0)

def show_gui():
    global main_root, devices
    root = tk.Tk()
    main_root = root
    root.title("Ping Alert+ v9.0 - Concurrent")
    root.geometry("600x600")
    root.minsize(550, 550)

    style = ttk.Style(); style.configure("TNotebook.Tab", padding=[10, 5], font=('Calibri', 10))
    
    tab_control = ttk.Notebook(root)
    tab_monitor = ttk.Frame(tab_control, padding=10)
    tab_config = ttk.Frame(tab_control, padding=10)
    tab_about = ttk.Frame(tab_control, padding=10)
    tab_control.add(tab_monitor, text='Monitoring'); tab_control.add(tab_config, text='Configuration'); tab_control.add(tab_about, text='About')
    tab_control.pack(expand=1, fill='both', padx=5, pady=5)

    # --- Monitoring Tab ---
    def add_device_ui():
        ip, name = ip_entry.get().strip(), name_entry.get().strip()
        if ip and name:
            if any(d['ip'] == ip for d in devices): messagebox.showwarning("Duplicate", f"IP {ip} is already in the list."); return
            devices.append({'ip': ip, 'name': name})
            listbox.insert(tk.END, f"{name} ({ip})")
            ip_entry.delete(0, tk.END); name_entry.delete(0, tk.END)

    def delete_selected_ui():
        if not listbox.curselection(): return
        if messagebox.askyesno("Confirm", "Delete the selected device(s) from the current list?"):
            for index in sorted(listbox.curselection(), reverse=True):
                listbox.delete(index); del devices[index]

    def save_list_as():
        list_name = simpledialog.askstring("Save List", "Enter a name for this device list:")
        if list_name:
            file_path = os.path.join(LISTS_DIR, f"{list_name}.json")
            with open(file_path, "w", encoding='utf-8') as f: json.dump(devices, f, indent=4, ensure_ascii=False)
            messagebox.showinfo("Saved", f"Device list '{list_name}' has been saved.")

    def load_list_window():
        win = tk.Toplevel(root); win.title("Load Saved List"); win.geometry("350x300"); win.transient(root)
        ttk.Label(win, text="Select a list to load:").pack(pady=5)
        saved_lists = [f for f in os.listdir(LISTS_DIR) if f.endswith('.json')]
        list_box = tk.Listbox(win); list_box.pack(fill='both', expand=True, padx=5, pady=5)
        for f_name in saved_lists: list_box.insert(tk.END, os.path.splitext(f_name)[0])

        def on_load():
            if not list_box.curselection(): return
            list_name = list_box.get(list_box.curselection())
            file_path = os.path.join(LISTS_DIR, f"{list_name}.json")
            global devices
            try:
                with open(file_path, "r", encoding='utf-8') as f: devices = json.load(f)
                listbox.delete(0, tk.END)
                for dev in devices: listbox.insert(tk.END, f"{dev['name']} ({dev['ip']})")
                win.destroy()
            except (json.JSONDecodeError, FileNotFoundError) as e: messagebox.showerror("Error", f"Failed to load list: {e}", parent=win)
        
        ttk.Button(win, text="Load Selected", command=on_load).pack(pady=10)

    monitor_top_frame = ttk.Frame(tab_monitor); monitor_top_frame.pack(fill='x', pady=5)
    ttk.Label(monitor_top_frame, text="IP:").grid(row=0, column=0, padx=5, pady=5, sticky='w')
    ip_entry = ttk.Entry(monitor_top_frame, width=30); ip_entry.grid(row=0, column=1, padx=5, pady=5)
    ttk.Label(monitor_top_frame, text="Name:").grid(row=1, column=0, padx=5, pady=5, sticky='w')
    name_entry = ttk.Entry(monitor_top_frame, width=30); name_entry.grid(row=1, column=1, padx=5, pady=5)

    list_frame = ttk.LabelFrame(tab_monitor, text="Device List"); list_frame.pack(pady=5, padx=5, fill='both', expand=True)
    listbox = tk.Listbox(list_frame, selectmode=tk.EXTENDED); listbox.pack(pady=5, padx=5, fill='both', expand=True)

    monitor_button_frame = ttk.Frame(tab_monitor); monitor_button_frame.pack(fill='x', pady=5)
    ttk.Button(monitor_button_frame, text="Add Device", command=add_device_ui).pack(side='left', padx=5)
    ttk.Button(monitor_button_frame, text="Delete Selected", command=delete_selected_ui).pack(side='left', padx=5)
    ttk.Button(monitor_button_frame, text="Save List As...", command=save_list_as).pack(side='left', padx=5)
    ttk.Button(monitor_button_frame, text="Load Saved List", command=load_list_window).pack(side='left', padx=5)

    bottom_frame = ttk.Frame(tab_monitor); bottom_frame.pack(fill='x', side='bottom', pady=10)
    stop_btn = ttk.Button(bottom_frame, text="Stop", command=stop_monitoring, state="disabled"); stop_btn.pack(side='right', padx=5)
    start_btn = ttk.Button(bottom_frame, text="Start", command=lambda: start_monitoring(listbox, start_btn, stop_btn)); start_btn.pack(side='right')
    ttk.Button(bottom_frame, text="Hide to Tray", command=hide_to_tray).pack(side='left', padx=5)

    # --- Configuration Tab ---
    config_main_frame = ttk.Frame(tab_config)
    config_main_frame.pack(fill='both', expand=True)

    tg_frame = ttk.LabelFrame(config_main_frame, text="Telegram Settings")
    tg_frame.pack(fill='x', pady=10, padx=5)
    ttk.Label(tg_frame, text="Bot Token:").grid(row=0, column=0, sticky='w', padx=5, pady=5)
    token_entry = ttk.Entry(tg_frame, show="*", width=45)
    token_entry.grid(row=0, column=1, padx=5, pady=5)
    ttk.Label(tg_frame, text="Chat ID:").grid(row=1, column=0, sticky='w', padx=5, pady=5)
    chat_id_entry = ttk.Entry(tg_frame, width=45)
    chat_id_entry.grid(row=1, column=1, padx=5, pady=5)
    
    tg_button_frame = ttk.Frame(tg_frame)
    tg_button_frame.grid(row=2, column=0, columnspan=2, pady=10)

    def save_telegram_config():
        config = {"bot_token": token_entry.get(), "chat_id": chat_id_entry.get()}
        with open(CONFIG_FILE, "w") as f: json.dump(config, f)
        messagebox.showinfo("Telegram", "Telegram configuration saved.")

    def test_telegram():
        try:
            bot_token, chat_id = token_entry.get(), chat_id_entry.get()
            if not bot_token or not chat_id: messagebox.showerror("Error", "Bot Token and Chat ID are required."); return
            requests.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", data={"chat_id": chat_id, "text": "Test message from Ping Alert+."}, timeout=10)
            messagebox.showinfo("Success", "Test message sent to Telegram.")
        except Exception as e: messagebox.showerror("Error", f"Failed to send message:\n{e}")

    def delete_telegram_config():
        if os.path.exists(CONFIG_FILE) and messagebox.askyesno("Confirm", "Delete Telegram config?"):
            os.remove(CONFIG_FILE); token_entry.delete(0, tk.END); chat_id_entry.delete(0, tk.END)
            messagebox.showinfo("Telegram", "Telegram configuration deleted.")

    ttk.Button(tg_button_frame, text="Save", command=save_telegram_config).pack(side='left', padx=5)
    ttk.Button(tg_button_frame, text="Test", command=test_telegram).pack(side='left', padx=5)
    ttk.Button(tg_button_frame, text="Delete", command=delete_telegram_config).pack(side='left', padx=5)

    email_frame = ttk.LabelFrame(config_main_frame, text="Email Settings")
    email_frame.pack(fill='x', pady=10, padx=5)
    email_fields = ["SMTP Server:", "SMTP Port:", "Email:", "Password:", "Receiver:"]
    email_entries = {}
    for i, field in enumerate(email_fields):
        ttk.Label(email_frame, text=field).grid(row=i, column=0, sticky='w', padx=5, pady=2)
        entry = ttk.Entry(email_frame, width=45, show="*" if field == "Password:" else ""); entry.grid(row=i, column=1, padx=5, pady=2)
        email_entries[field] = entry

    def save_email_config():
        config = {k.replace(':','').replace(' ','_').lower(): v.get() for k, v in email_entries.items()}
        with open(EMAIL_CONFIG_FILE, "w") as f: json.dump(config, f)
        messagebox.showinfo("Email", "Email configuration saved.")
    def test_email():
        try:
            msg = EmailMessage(); msg.set_content("This is a test email from Ping Alert+."); msg["Subject"] = "Ping Alert+ Test"
            msg["From"] = email_entries["Email:"].get(); msg["To"] = email_entries["Receiver:"].get()
            with smtplib.SMTP(email_entries["SMTP Server:"].get(), int(email_entries["SMTP Port:"].get())) as s:
                s.starttls(); s.login(email_entries["Email:"].get(), email_entries["Password:"].get()); s.send_message(msg)
            messagebox.showinfo("Success", "Test email sent.")
        except Exception as e: messagebox.showerror("Error", f"Failed to send email:\n{e}")
    def delete_email_config():
        if os.path.exists(EMAIL_CONFIG_FILE) and messagebox.askyesno("Confirm", "Delete Email config?"):
            os.remove(EMAIL_CONFIG_FILE)
            for entry in email_entries.values(): entry.delete(0, tk.END)
            messagebox.showinfo("Email", "Email configuration deleted.")

    email_button_frame = ttk.Frame(email_frame)
    email_button_frame.grid(row=len(email_fields), column=0, columnspan=2, pady=10)
    ttk.Button(email_button_frame, text="Save", command=save_email_config).pack(side='left', padx=5)
    ttk.Button(email_button_frame, text="Test", command=test_email).pack(side='left', padx=5)
    ttk.Button(email_button_frame, text="Delete", command=delete_email_config).pack(side='left', padx=5)

    def load_configs():
        if os.path.exists(CONFIG_FILE): # Load Telegram
            with open(CONFIG_FILE, "r") as f: 
                try: config = json.load(f); token_entry.insert(0, config.get("bot_token", "")); chat_id_entry.insert(0, config.get("chat_id", ""))
                except json.JSONDecodeError: pass
        if os.path.exists(EMAIL_CONFIG_FILE): # Load Email
            with open(EMAIL_CONFIG_FILE, "r") as f:
                try: 
                    config = json.load(f)
                    email_entries["SMTP Server:"].insert(0, config.get("smtp_server", ""))
                    email_entries["SMTP Port:"].insert(0, config.get("smtp_port", ""))
                    email_entries["Email:"].insert(0, config.get("email", ""))
                    email_entries["Password:"].insert(0, config.get("password", ""))
                    email_entries["Receiver:"].insert(0, config.get("receiver", ""))
                except json.JSONDecodeError: pass
    
    load_configs()

    # --- About Tab ---
    about_text = ("Ping Alert+ v9.1\n\n" "Developed by Alihan Aslıyüce & Gemini\n" "Contact: alihan.asliyuce@gmail.com")
    ttk.Label(tab_about, text=about_text, justify='left', font=("Calibri", 11)).pack(padx=10, pady=10, anchor='n')

    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()

if __name__ == "__main__":
    show_gui()
