from flask import Flask, redirect, render_template, request as req, send_from_directory
from winreg import HKEY_CLASSES_ROOT, HKEY_CURRENT_USER, OpenKey, QueryValueEx
from localStoragePy import localStoragePy as lsp
from flask_socketio import SocketIO
from flaskwebgui import FlaskUI
from PIL import ImageGrab
from ctypes import WinDLL 
from typing import List
import numpy as np
import pytesseract
import regex as re
import screeninfo
import threading
import jellyfish
import os, sys
import json
import cv2

# Some constants
def global_constants():
    naughty_dict = {
        "an A+ rating or better in 10 Field or Siege Battles of any type",
        "Join 10 Ranked Battles or win 10 Field or Siege Battles",
        "in Field or Siege Battles",
        "in 8 Siege Battles",
    }
    url = "https://cbimg.salamski.com"
    headers = {"content-type": "image/png"}
    max_quest_lenth = 110
    pytesseract.pytesseract.tesseract_cmd = resource_path("./Tesseract-OCR/tesseract.exe")
    return naughty_dict, url, headers, max_quest_lenth

# Production path.
def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

# Check if already running
def instance_check():
    U32DLL = WinDLL("user32")
    hwnd = U32DLL.FindWindowW(None, "CBQuestTracker")
    if hwnd:
        U32DLL.ShowWindow(hwnd, 5)
        U32DLL.SetForegroundWindow(hwnd)
        sys.exit(0)
    return True

# Get default browser on windows
def get_system_default_browser():
    chosen_browser = None
    try:
        with OpenKey(HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice') as regkey:
            browser_choice = QueryValueEx(regkey, 'ProgId')[0]

        with OpenKey(HKEY_CLASSES_ROOT, r'{}\shell\open\command'.format(browser_choice)) as regkey:
            browser_path_tuple = QueryValueEx(regkey, None)
            chosen_browser = browser_path_tuple[0].split('"')[1]    
    except Exception:
        print('Failed to look up default browser in system registry. Using fallback value.')
    return chosen_browser

# Model class, preforms all operations on the state. 
class Model:
    def __init__(self, socketio: SocketIO):
        self.vocabulary = self.__read_vocab()
        self.db = lsp("CBQuestTracker", "json")
        self.stop_event = threading.Event()
        self.sync_thread = None
        self.stop_event.set()
        self.io = socketio
        self.__scan_monitors()

    # App State
    def __scan_monitors(self):
        monitors = screeninfo.get_monitors()
        nbr_mons = len(monitors)
        db = self.__read_state()

        if nbr_mons > 1:
            chosen_screen_index = db['screen']
            if 0 <= chosen_screen_index < nbr_mons:
                self.force_screen_pick = False
            else:
                self.force_screen_pick = True
        else:
            # Very bad...
            mon = monitors[0]
            if not mon or not mon.is_primary:
                sys.exit("How is there no monitor connected!!!")

            db.update({"screen" : 0})
            self.__write_state(**db)
            self.force_screen_pick = False
        self.mons = monitors
                
    def __read_vocab(self):
        vocabName = resource_path("vocabulary.json")
        if os.path.isfile(vocabName):
            with open(vocabName, "r") as f:
                vocab = json.load(f)
                f.close()
        else:
            vocab = []
        return vocab

    def __read_state(self):
        db = self.db.getItem("db")
        if db is not None and "quests" in db and "duplicates" in db and "done" in db and "window" in db and "screen" in db:
            return json.loads(db)
        else:
            self.__write_state([])
            return self.__read_state()

    def __write_state(self, quests, duplicates=[], done=[], window=[0,0,725,950], screen=0):
        db = json.dumps({"quests": quests, "duplicates": duplicates, "done": done, "window": window, "screen":screen})
        self.db.setItem("db",db)

    def get_state(self):
        return self.__read_state()


    # Sync Thread
    def __process_img(self, img):
        mask_white = cv2.inRange(img, np.array([200, 200, 200]), np.array([255, 255, 255]))
        mask_blue = cv2.inRange(img, np.array([50, 40, 10]), np.array([250, 180, 40]))
        mask = mask_white + mask_blue
        img = cv2.bitwise_and(img, img, mask=mask)

        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = cv2.normalize(img, None, 0, 255, cv2.NORM_MINMAX)
        img = cv2.GaussianBlur(img, (1, 1), 0)
        img = cv2.threshold(img, 100, 255, cv2.THRESH_BINARY)[1]
        return img

    def __text_to_quests(self, text):
        split = list(filter(None, re.split('\n\n', text)))
        quests = [re.sub(r"[\d_,]+\/|(?<=\d),(?=\d)", "", re.sub(r"\s", " ", s).strip()) for s in split]
        quests = list(filter(None, quests))
        return quests

    def __grab_quests_from_screen(self):
        img = np.array(ImageGrab.grab(bbox=(1200, 435, 1850, 890)))
        img = self.__process_img(img)
        return self.__text_to_quests(pytesseract.image_to_string(img, lang="eng", config="--psm 4"))

    def __score_quest(self, quest):
        if quest[-1] != "." and len(quest) < max_quest_lenth:
            if quest[-1] == ",":
                temp = list(quest)
                temp[-1] = "."
                quest = "".join(temp)
            else:
                quest += "."

        quest = re.sub(r"(?<=[SABCD])t", "+", quest)
        quest = re.sub("1or", "1 or", quest)
        quest = re.sub("Arating", "A rating", quest)

        levenshtein_threshold = 5
        if re.sub(r"[.,]", "", quest) in naughty_dict:
            levenshtein_threshold = 1

        scores = [
            (
                jellyfish.levenshtein_distance(
                    quest[:max_quest_lenth], entry[:max_quest_lenth]
                ),
                entry,
            )
            for entry in self.vocabulary
        ]
        scores = list(filter(lambda t: t[0] <= levenshtein_threshold, scores))
        scores.sort()
        return scores, quest

    def __add_quest_to_dict(self, new, dq, pd):
        stripped_new = re.sub(r"\d+", "", new)
        for q in dq:
            stripped_q = re.sub(r"\d+", "", q)
            if stripped_new == stripped_q:
                index = -1 
                for i, dups in enumerate(pd):
                    for dup in dups:
                        if dup == q:
                            index = i
                            break
                
                if index > -1:
                    pd[index] = sorted(pd[index] + [new])
                else:
                    pd += [sorted([new, q])]
                return
        dq.add(new)

    def __flatten_dups(self, pd):
        return [dup for dup_list in pd for dup in dup_list]

    def __sync_with_game(self):
        dq = set()
        pd = []
        db = self.__read_state()
        db.update({"quests":list(dq), "duplicates" : pd, "done": []})
        self.__write_state(**db)
        while not self.stop_event.is_set():
            for quest in self.__grab_quests_from_screen():

                scores, quest = self.__score_quest(quest)
                if len(scores) <= 0:
                    continue

                entry_to_add = min(scores)
                exists_in_dq = entry_to_add[1] not in dq
                exists_in_pd = entry_to_add[1] not in self.__flatten_dups(pd)
                if exists_in_dq and exists_in_pd:
                    old_dq = len(dq)
                    old_pd = len(self.__flatten_dups(pd))
                    self.__add_quest_to_dict(entry_to_add[1], dq, pd)
                    if len(dq) - old_dq > 0 or len(self.__flatten_dups(pd)) - old_pd:
                        db = self.__read_state()
                        db.update({"quests":list(dq), "duplicates" : pd, "done": []})
                        self.__write_state(**db)
                        self.io.emit("new_quest", {"dq" : list(dq), "pd" : pd})

    def start_sync_thread(self):
        self.stop_event.clear()
        self.sync_thread = threading.Thread(target=self.__sync_with_game)
        self.sync_thread.daemon = True
        self.sync_thread.start()


    # Serverside Actions
    def update_sorted_list(self, input):
        db = self.__read_state()
        if sorted(input) == sorted(db['quests']):
            db.update({"quests":input})
            self.__write_state(**db)
            return True
        else:
            return False

    def mark_quest_done(self, index):
        db = self.__read_state()
        quests = db['quests']
        done = db['done']
        if 0 <= index < len(quests):
            match = quests[index]
            quests.remove(match)
            done = [match] + done
            db.update({"quests":quests, "done":done})
            self.__write_state(**db)
            return True
        return False
    
    def unmark_quest_done(self, form):
        db = self.__read_state()
        quests = db['quests']
        done = db['done']
        if "done" in form and form['done'] in done:
            done.remove(form['done'])
            quests.append(form['done'])
            db.update({"quests":quests, "done":done})
            self.__write_state(**db)
            return True
        return False

    def __find_duplicates_in_quests(self, quests, dups):
        for dup in dups[0]:
            for index, quest in enumerate(quests):
                if quest == dup:
                    return index,dup
        return -1, ""
    
    def remove_duplicates(self, form):
        db = self.__read_state()
        quests = db['quests']
        dups = db['duplicates']
        if "dup" in form:
            dup = form['dup']
            pair = dups[0]
            if dup in pair:
                index, found = self.__find_duplicates_in_quests(quests,dups)
                if index > -1:
                    if found != dup:
                        quests[index] = dup
                    dups = dups[1:]
                    db.update({"quests": quests, "duplicates":dups})
                    self.__write_state(**db)
                    return True
        return False

    def save_last_window_cords(self, cords):
        db = self.__read_state()
        saved_cords = db['window']
        if len(cords) == 4 and cords != saved_cords:
            db.update({"window": cords})
            self.__write_state(**db)
            return True
        return False

# Define served routes, act as Controller.
def define_routes(app):

    @app.route('/')
    def hello_world():
        db = m.get_state()
        return render_template("index.html", quests=db['quests'], dups=db['duplicates'], doneQ=db['done'], not_syncing=m.stop_event.is_set())
    
    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(resource_path('static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

    @app.route("/sync", methods=['POST'])
    def start_sync():
        db = m.get_state()
        if not m.force_screen_pick and m.stop_event.is_set() and not (len(db['duplicates']) > 0):
            m.start_sync_thread()
            return redirect("/")
        return "BAD!!!", 404
    
    @app.route("/stop", methods=['POST'])
    def stop_sync():
        if not m.force_screen_pick and not m.stop_event.is_set():
            m.stop_event.set()
            return redirect("/")
        return "BAD!!!", 404
    
    @app.route("/update", methods=['POST'])
    def update_list():
        db = m.get_state()
        if not m.force_screen_pick and m.stop_event.is_set() and not (len(db['duplicates']) > 0)  and m.update_sorted_list(req.json):
            return "OK!", 200
        return "BAD!!!", 404
        
    @app.route("/done/<int:index>", methods=['POST'])
    def quest_done(index):
        db = m.get_state()
        if not m.force_screen_pick and m.stop_event.is_set() and not (len(db['duplicates']) > 0)  and m.mark_quest_done(index):
            return redirect(f"/")
        return "BAD!!!", 404
        
    @app.route("/undone", methods=['POST'])
    def quest_undone():
        db = m.get_state()
        if not m.force_screen_pick and m.stop_event.is_set() and not (len(db['duplicates']) > 0)  and m.unmark_quest_done(req.form):
            return redirect("/")
        return "BAD!!!", 404

    @app.route("/dups", methods=['post'])
    def remove_duplicate():
        db = m.get_state()
        if not m.force_screen_pick and m.stop_event.is_set() and m.remove_duplicates(req.form): 
            return redirect("/")
        return "BAD!!!", 404
    
    @app.route("/window", methods=['POST'])
    def recive_window_info():
        if m.save_last_window_cords(req.json):
            return redirect("/")
        return "BAD!!!", 404


if __name__ == "__main__" and instance_check():
    naughty_dict, url, headers, max_quest_lenth = global_constants()

    # Flask API as Controller, serves HTML as View
    app = Flask(__name__, template_folder=resource_path("./templates"), static_folder=resource_path("./static"))
    define_routes(app)
    socketio = SocketIO(app, async_mode='gevent')

    # Model class containing all the logic.
    m = Model(socketio)
    db = m.get_state()

    # display the website in a isolated tab, current default browser is used. 
    flaskui = FlaskUI(app=app, socketio=socketio, server="flask_socketio", browser_path=get_system_default_browser(), fullscreen=False, width=db['window'][2], height=db['window'][3])    
    flaskui.browser_command.append(f"--window-position={",".join(list(map(str,db['window'][:2])))}")
    flaskui.run()
