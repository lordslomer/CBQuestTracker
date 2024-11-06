from winreg import HKEY_CLASSES_ROOT, HKEY_CURRENT_USER, OpenKey, QueryValueEx
from flask import Flask, redirect, render_template, request as req, send_from_directory
from localStoragePy import localStoragePy as lsp
from flask_socketio import SocketIO
from flaskwebgui import FlaskUI
from PIL import ImageGrab
from ctypes import WinDLL 
from typing import List
import numpy as np
import pytesseract
import regex as re
import threading
import jellyfish
import os, sys
import json
import cv2


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


def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def instance_check():
    U32DLL = WinDLL("user32")
    hwnd = U32DLL.FindWindowW(None, "CBQuestTracker")
    if hwnd:
        U32DLL.ShowWindow(hwnd, 5)
        U32DLL.SetForegroundWindow(hwnd)
        sys.exit(0)
    return True


def get_system_default_browser():
    chosen_browser = None
    try:
        from winreg import HKEY_CLASSES_ROOT, HKEY_CURRENT_USER, OpenKey, QueryValueEx

        with OpenKey(HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice') as regkey:
            # Get the user choice
            browser_choice = QueryValueEx(regkey, 'ProgId')[0]

        with OpenKey(HKEY_CLASSES_ROOT, r'{}\shell\open\command'.format(browser_choice)) as regkey:
            # Get the application the user's choice refers to in the application registrations
            browser_path_tuple = QueryValueEx(regkey, None)

            # This is a bit sketchy and assumes that the path will always be in double quotes
            chosen_browser = browser_path_tuple[0].split('"')[1]

    except Exception:
        print('Failed to look up default browser in system registry. Using fallback value.')
    return chosen_browser


class Model:
    def __init__(self, socketio: SocketIO):
        self.vocabulary = self.__read_vocab()
        self.db = lsp("CBQuestTracker", "json")
        self.stop_event = threading.Event()
        self.sync_thread = None
        self.stop_event.set()
        self.io = socketio
        self.__read_state()

    def __read_vocab(self):
        dbName = resource_path("vocabulary.json")
        if os.path.isfile(dbName):
            with open(dbName, "r") as f:
                db = json.load(f)
                f.close()
        else:
            db = []
        return db

    def __read_state(self):
        db = self.db.getItem("db")
        if db is not None:
            db = json.loads(db)
            self.quests: List[str] = list(db["quests"])
            self.duplicates = db["duplicates"]
            self.done = db["done"]
            self.last_window_cords = db['lastWindowCords']
        else:
            self.__write_state(set())

    def __write_state(self, quests, duplicates=[], done=[], last_window_cords=[0,0]):
        if isinstance(quests, set):
            self.quests: List[str] = list(quests)
        elif isinstance(quests, list):
            self.quests: List[str] = quests

        self.duplicates = duplicates
        self.done = done
        self.last_window_cords = last_window_cords
        self.db.setItem(
            "db",
            json.dumps({"quests": self.quests, "duplicates": self.duplicates, "done": self.done, "lastWindowCords": self.last_window_cords}),
        )

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
        self.__write_state(dq, last_window_cords=self.last_window_cords)
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
                        self.__write_state(dq, pd, last_window_cords=self.last_window_cords)
                        self.io.emit("new_quest", {"dq" : self.quests, "pd" : self.duplicates})

    def start_sync_thread(self):
        self.stop_event.clear()
        self.sync_thread = threading.Thread(target=self.__sync_with_game)
        self.sync_thread.daemon = True
        self.sync_thread.start()

    def update_sorted_list(self, input):
        if sorted(input) == sorted(self.quests):
            self.__write_state(input, self.duplicates, self.done, self.last_window_cords)
            return True
        else:
            return False

    def mark_quest_done(self, index):
        if 0 <= index < len(self.quests):
            match = self.quests[index]
            self.quests.remove(match)
            self.done = [match] + self.done
            self.__write_state(self.quests ,self.duplicates, self.done, self.last_window_cords)
            return True
        return False
    
    def unmark_quest_done(self, form):
        if "done" in form and form['done'] in self.done:
            self.done.remove(form['done'])
            self.quests.append(form['done'])
            self.__write_state(self.quests ,self.duplicates, self.done, self.last_window_cords)
            return True
        return False

    def __find_duplicates_in_quests(self):
        for dup in self.duplicates[0]:
            for index, quest in enumerate(self.quests):
                if quest == dup:
                    return index,dup
        return -1, ""
    
    def remove_duplicates(self, form):
        if "dup" in form:
            dup = form['dup']
            pair = self.duplicates[0]
            if dup in pair:
                index, found = self.__find_duplicates_in_quests()
                if index > -1:
                    if found != dup:
                        self.quests[index] = dup
                    self.duplicates = self.duplicates[1:]
                    self.__write_state(self.quests, self.duplicates, self.done, self.last_window_cords)
                    return True
        return False

    def save_last_window_cords(self, cords):
        if len(cords) == 2 and isinstance(cords[0], int) and isinstance(cords[1], int) and cords != self.last_window_cords:
            self.__write_state(self.quests, self.duplicates, self.done, cords)
            return True
        return False

    
if __name__ == "__main__" and instance_check():
    naughty_dict, url, headers, max_quest_lenth = global_constants()

    # import screeninfo
    # print(screeninfo.get_monitors())

    app = Flask(__name__, template_folder=resource_path("./templates"), static_folder=resource_path("./static"))
    socketio = SocketIO(app, async_mode='gevent')

    m = Model(socketio)
    
    @app.route('/')
    def hello_world():
        return render_template("index.html", quests=m.quests, dups=m.duplicates, doneQ=m.done, not_syncing=m.stop_event.is_set())
    
    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(resource_path('static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

    @app.route("/sync", methods=['POST'])
    def start_sync():
        if m.stop_event.is_set() and not (len(m.duplicates) > 0):
            m.start_sync_thread()
            return redirect("/")
        return "BAD!!!", 404
    
    @app.route("/stop", methods=['POST'])
    def stop_sync():
        if not m.stop_event.is_set():
            m.stop_event.set()
            return redirect("/")
        return "BAD!!!", 404
    
    @app.route("/update", methods=['POST'])
    def update_list():
        if m.stop_event.is_set() and m.update_sorted_list(req.json):
            return redirect("/")
        return "BAD!!", 400
        
    @app.route("/done/<int:index>", methods=['POST'])
    def quest_done(index):
        if m.stop_event.is_set() and m.mark_quest_done(index):
            return redirect(f"/")
        return "BAD!!!", 404
        
    @app.route("/undone", methods=['POST'])
    def quest_undone():
        if m.stop_event.is_set() and m.unmark_quest_done(req.form):
            return redirect("/")
        return "BAD!!!", 404

    @app.route("/dups", methods=['post'])
    def remove_duplicate():
        if m.stop_event.is_set() and m.remove_duplicates(req.form): 
            return redirect("/")
        return "BAD!!!", 404
    
    @app.route("/windowInfo", methods=['POST'])
    def recive_window_info():
        if m.save_last_window_cords(req.json):
            return redirect("/")
        else:
            return "BAD!!!", 404

    # socketio.run(app=app, host="0.0.0.0", port=3000)
    flaskui = FlaskUI(app=app, socketio=socketio, server="flask_socketio", browser_path=get_system_default_browser(), fullscreen=False, width=725, height=950)
    flaskui.browser_command.append(f"--window-position={",".join(list(map(str,m.last_window_cords)))}")
    flaskui.run()
