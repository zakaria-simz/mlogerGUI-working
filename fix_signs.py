with open("cache.txt", "r")as f:
    c = f.read()
    c = c.replace("+ -", "- ")

with open("cache2.txt", "w")as f:
    f.write(c)