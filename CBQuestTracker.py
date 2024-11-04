from localStoragePy import localStoragePy as lsp
from typing import List, Tuple
from ctypes import WinDLL  # TODO
import customtkinter as ctk
from PIL import ImageGrab
from enum import Enum
from tkinter import *
import numpy as np
import regex as re
import threading
import jellyfish
import requests
import os, sys
import json
import cv2


class Colors(Enum):
    btn_fg_red = ["#d03b3b", "#a51f1f"]
    btn_hv_red = ["#9f3636", "#701414"]
    btn_fg_blue = ["#3B8ED0", "#1F6AA5"]
    btn_hv_blue = ["#36719F", "#144870"]


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
    return naughty_dict, url, headers, max_quest_lenth


def resource_path(relative_path):
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def centerAppOnCreation(Screen, width: int, height: int, scale_factor: float = 1.0):
    screen_width = Screen.winfo_screenwidth()
    screen_height = Screen.winfo_screenheight()
    x = int(((screen_width / 2) - (width / 2)) * scale_factor)
    y = int(((screen_height / 2) - (height / 1.5)) * scale_factor)
    return f"{width}x{height}+{x}+{y}"


class Model:
    def __init__(self):
        self.vocabulary = requests.get(url + "/quests").json()
        self.stop_event = threading.Event()
        self.sync_thread = None
        self.stop_event.set()
        self.db = lsp("CBQuestTracker", "json")
        self.__read_state()

    def __read_state(self):
        db = self.db.getItem("db")
        if db is not None:
            db = json.loads(db)
            self.quests: List[Tuple[int, str]] = list(map(tuple, db["quests"]))
            self.duplicates = db["duplicates"]
        else:
            self.__write_state(set(), [])

    def __write_state(self, dq, pd):
        if isinstance(dq, set):
            self.quests: List[Tuple[int, str]] = list(enumerate(dq))
        elif isinstance(dq, list):
            self.quests: List[Tuple[int, str]] = dq

        self.duplicates = pd
        self.db.setItem(
            "db",
            json.dumps({"quests": list(self.quests), "duplicates": self.duplicates}),
        )

    def __send_screen(self):
        img = np.array(ImageGrab.grab(bbox=(1200, 435, 1850, 890)))
        _, img_png = cv2.imencode(".png", img)
        img_string = img_png.tobytes()
        r = requests.post(url + "/upload", data=img_string, headers=headers)
        return r.json()

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

    def __clean_duplicates(self, dq, pd):
        dups_to_delete_from_detected = []
        for q in dq:
            rest = dq.copy()
            rest.remove(q)
            stripped_q = re.sub(r"\d+", "", q)
            duplicates = list(
                filter(lambda r: stripped_q == re.sub(r"\d+", "", r), rest)
            )
            dup_pair = sorted([q] + duplicates)
            if len(duplicates) > 0:
                if q not in dups_to_delete_from_detected:
                    dups_to_delete_from_detected += duplicates
                if dup_pair not in pd:
                    # print("Found and flagged duplicate")
                    pd.append(dup_pair)
        dq -= set(dups_to_delete_from_detected)

    def __flatten_dups(self, pd):
        return [dup for dup_list in pd for dup in dup_list]

    def __sync_with_game(self, update_list_with_quests):
        dq = set()
        pd = []
        self.__write_state(dq, pd)
        while not self.stop_event.is_set():
            for quest in self.__send_screen():

                scores, quest = self.__score_quest(quest)
                if len(scores) <= 0:
                    continue

                entry_to_add = min(scores)
                exists_in_dq = entry_to_add[1] not in dq
                exists_in_pd = entry_to_add[1] not in self.__flatten_dups(pd)
                if exists_in_dq and exists_in_pd:
                    old_length = len(dq)
                    dq.add(entry_to_add[1])
                    self.__clean_duplicates(dq, pd)
                    if len(dq) - old_length > 0:
                        self.__write_state(dq, pd)
                        update_list_with_quests(self.get_data())

    def start_sync_thread(self, update_list_with_quests):
        self.stop_event.clear()
        self.sync_thread = threading.Thread(
            target=self.__sync_with_game, args=(update_list_with_quests,)
        )
        self.sync_thread.daemon = True
        self.sync_thread.start()

    def get_data(self):
        return self.quests


class View:
    def __init__(self, root: ctk.CTk, c):
        self.root = root
        self.c = c
        self.my_font = ctk.CTkFont(size=20)

        self.root.grid_columnconfigure((0, 1), weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        self.statF = self.StatusFrame(master=root, font=self.my_font)
        self.statF.grid(row=0, column=0, padx=20, pady=20, sticky="ew")

        self.syncF = self.SyncFrame(
            master=root, sync_pressed=self.c.sync_pressed, font=self.my_font
        )
        self.syncF.grid(row=0, column=1, padx=20, pady=20, sticky="ew")

        self.q_list = self.QuestList(master=root)
        self.q_list.grid(row=1, column=0, padx=20, sticky="ewsn", columnspan=2)

    def update_sync_button(self, not_syncing: bool):
        sb = self.syncF.sync_button
        if not_syncing:
            sb.configure(text="Stop")
            sb.configure(fg_color=Colors.btn_fg_red.value)
            sb.configure(hover_color=Colors.btn_hv_red.value)
        else:
            sb.configure(text="Sync")
            sb.configure(fg_color=Colors.btn_fg_blue.value)
            sb.configure(hover_color=Colors.btn_hv_blue.value)

    def update_list_with_quests(self, quests: List[Tuple[int, str]]):
        quests.sort()

        self.q_list.clear_widgets()
        for index, quest in quests:
            self.q_list.add_quest(index, quest)

    class StatusFrame(ctk.CTkFrame):
        def __init__(self, font, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.grid_columnconfigure(0, weight=1)

            self.nbr_of_quests = ctk.CTkLabel(self, font=font)
            self.nbr_of_quests.grid(row=0, column=0, padx=20, pady=20)

    class SyncFrame(ctk.CTkFrame):

        def __init__(self, sync_pressed, font, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.grid_columnconfigure(0, weight=1)

            self.sync_button = ctk.CTkButton(
                self, text="Sync", command=sync_pressed, font=font
            )
            self.sync_button.grid(row=0, column=0, padx=20, pady=20)

    class QuestList(ctk.CTkScrollableFrame):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.my_font = ctk.CTkFont(size=15)
            self.widgets = []

        def add_quest(self, index: int, quest: str):
            widget = ctk.CTkLabel(
                master=self,
                text=quest,
                justify="left",
            )
            self.widgets.append(widget)
            widget.grid(row=index, column=0, padx=5, pady=5)

        def clear_widgets(self):
            for widget in self.widgets:
                widget.grid_forget()
            self.widgets = []


class Controller:
    def __init__(self, root: ctk.CTk):
        self.r = root
        self.m = Model()
        self.v = View(root, self)
        self.v.update_list_with_quests(self.m.get_data())

    def sync_pressed(self):
        not_syncing = self.m.stop_event.is_set()
        if not_syncing:
            self.m.start_sync_thread(self.v.update_list_with_quests)
            self.v.update_list_with_quests([])
        else:
            self.m.stop_event.set()
            self.v.update_list_with_quests(self.m.get_data())
        self.v.update_sync_button(not_syncing)


def instance_check():
    U32DLL = WinDLL("user32")
    hwnd = U32DLL.FindWindowW(None, "CBQuestTracker")
    if hwnd:
        U32DLL.ShowWindow(hwnd, 5)
        U32DLL.SetForegroundWindow(hwnd)
        sys.exit(0)
    return True


if __name__ == "__main__" and instance_check():
    naughty_dict, url, headers, max_quest_lenth = global_constants()

    ctk.set_appearance_mode("system")
    root = ctk.CTk()
    root.title("CBUnitQuestTracker")
    root.geometry(centerAppOnCreation(root, 750, 600, root._get_window_scaling()))


    app = Controller(root)
    root.mainloop()
