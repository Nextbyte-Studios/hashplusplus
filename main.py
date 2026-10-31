import os
import sys
import random
import math
import json

with open("config.json", "r") as file:
    config = json.load(file)

def printconfigsetting(name):
    x = config[name]

os.system('cls' if os.name == 'nt' else 'clear')

printconfigsetting("startupmessage")