# -*- coding: utf-8 -*-
import hashlib
import io
import os
import shutil

from core import httptools, filetools, downloadtools
from core.ziptools import ziptools
from platformcode import logger, platformtools
import json
import xbmc
import re
import xbmcaddon
from lib import githash

addon = xbmcaddon.Addon('plugin.video.kod')

_hdr_pat = re.compile("^@@ -(\d+),?(\d+)? \+(\d+),?(\d+)? @@.*")

branch = 'master'
user = 'kodiondemand'
repo = 'addon'
addonDir = os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + '/'
maxPage = 5  # le api restituiscono 30 commit per volta, quindi se si è rimasti troppo indietro c'è bisogno di andare avanti con le pagine
trackingFile = "last_commit.txt"


def loadCommits(page=1):
    apiLink = 'https://api.github.com/repos/' + user + '/' + repo + '/commits?sha=' + branch + "&page=" + str(page)
    logger.info(apiLink)
    # riprova ogni secondo finchè non riesce (ad esempio per mancanza di connessione)
    while True:
        try:
            commitsLink = httptools.downloadpage(apiLink).data
            ret = json.loads(commitsLink)
            break
        except:
            xbmc.sleep(1000)

    return ret


def check_addon_init():
    if not addon.getSetting('addon_update_enabled'):
        return False
    logger.info('Cerco aggiornamenti..')
    commits = loadCommits()

    try:
        localCommitFile = open(addonDir+trackingFile, 'r+')
    except:
        calcCurrHash()
        localCommitFile = open(addonDir + trackingFile, 'r+')
    localCommitSha = localCommitFile.read()
    localCommitSha = localCommitSha.replace('\n', '') # da testare
    logger.info('Commit locale: ' + localCommitSha)
    updated = False

    pos = None
    for n, c in enumerate(commits):
        if c['sha'] == localCommitSha:
            pos = n
            break
    else:
        # evitiamo che dia errore perchè il file è già in uso
        localCommitFile.close()
        calcCurrHash()
        return True

    if pos > 0:
        changelog = ''
        nCommitApplied = 0
        for c in reversed(commits[:pos]):
            commit = httptools.downloadpage(c['url']).data
            commitJson = json.loads(commit)
            # evitiamo di applicare i merge commit
            if 'Merge' in commitJson['commit']['message']:
                continue
            logger.info('aggiornando a ' + commitJson['sha'])
            alreadyApplied = True

            for file in commitJson['files']:
                if file["filename"] == trackingFile:  # il file di tracking non si modifica
                    continue
                else:
                    logger.info(file["filename"])
                    if file['status'] == 'modified' or file['status'] == 'added':
                        if 'patch' in file:
                            text = ""
                            try:
                                localFile = open(addonDir + file["filename"], 'r+')
                                text = localFile.read()
                            except IOError: # nuovo file
                                # crea le cartelle se non esistono
                                dirname = os.path.dirname(addonDir + file["filename"])
                                if not os.path.exists(dirname):
                                    os.makedirs(dirname)

                                localFile = open(addonDir + file["filename"], 'w')

                            patched = apply_patch(text, (file['patch']+'\n').encode('utf-8'))
                            if patched != text:  # non eseguo se già applicata (es. scaricato zip da github)
                                if getSha(patched) == file['sha']:
                                    localFile.seek(0)
                                    localFile.truncate()
                                    localFile.writelines(patched)
                                    localFile.close()
                                    alreadyApplied = False
                                else:  # nel caso ci siano stati problemi
                                    logger.info('lo sha non corrisponde, scarico il file')
                                    remove(addonDir + file["filename"])
                                    downloadtools.downloadfile(file['raw_url'], addonDir + file['filename'],
                                                               silent=True, continuar=True, resumir=False)
                        else:  # è un file NON testuale, lo devo scaricare
                            # se non è già applicato
                            if not (filetools.isfile(addonDir + file['filename']) and getSha(addonDir + file['filename']) == file['sha']):
                                remove(addonDir + file["filename"])
                                downloadtools.downloadfile(file['raw_url'], addonDir + file['filename'], silent=True,
                                                           continuar=True, resumir=False)
                                alreadyApplied = False
                    elif file['status'] == 'removed':
                        remove(addonDir+file["filename"])
                        alreadyApplied = False
                    elif file['status'] == 'renamed':
                        # se non è già applicato
                        if not (filetools.isfile(addonDir + file['filename']) and getSha(addonDir + file['filename']) == file['sha']):
                            dirs = file['filename'].split('/')
                            for d in dirs[:-1]:
                                if not filetools.isdir(addonDir + d):
                                    filetools.mkdir(addonDir + d)
                            filetools.move(addonDir + file['previous_filename'], addonDir + file['filename'])
                            alreadyApplied = False
            if not alreadyApplied:  # non mando notifica se già applicata (es. scaricato zip da github)
                changelog += commitJson['commit']['message'] + " | "
                nCommitApplied += 1
        if addon.getSetting("addon_update_message"):
            time = nCommitApplied * 2000 if nCommitApplied < 10 else 20000
            platformtools.dialog_notification('Kodi on Demand', 'Aggiornamenti applicati:\n' + changelog[:-3], time)

        localCommitFile.seek(0)
        localCommitFile.truncate()
        localCommitFile.writelines(c['sha'])
        localCommitFile.close()

    else:
        logger.info('Nessun nuovo aggiornamento')

    return updated


def calcCurrHash():
    treeHash = githash.tree_hash(addonDir).hexdigest()
    logger.info('tree hash: ' + treeHash)
    commits = loadCommits()
    lastCommitSha = commits[0]['sha']
    page = 1
    while commits and page <= maxPage:
        found = False
        for n, c in enumerate(commits):
             if c['commit']['tree']['sha'] == treeHash:
                localCommitFile = open(addonDir + trackingFile, 'w')
                localCommitFile.write(c['sha'])
                localCommitFile.close()
                found = True
                break
        else:
            page += 1
            commits = loadCommits(page)

        if found:
            break
    else:
        logger.info('Non sono riuscito a trovare il commit attuale, scarico lo zip')
        hash = updateFromZip()
        # se ha scaricato lo zip si trova di sicuro all'ultimo commit
        localCommitFile = open(addonDir + trackingFile, 'w')
        localCommitFile.write(hash if hash else lastCommitSha)
        localCommitFile.close()


# https://gist.github.com/noporpoise/16e731849eb1231e86d78f9dfeca3abc  Grazie!

def apply_patch(s,patch,revert=False):
  """
  Apply unified diff patch to string s to recover newer string.
  If revert is True, treat s as the newer string, recover older string.
  """
  s = s.splitlines(True)
  p = patch.splitlines(True)
  t = ''
  i = sl = 0
  (midx,sign) = (1,'+') if not revert else (3,'-')
  while i < len(p) and p[i].startswith(("---","+++")): i += 1 # skip header lines
  while i < len(p):
    m = _hdr_pat.match(p[i])
    if not m: raise Exception("Cannot process diff")
    i += 1
    l = int(m.group(midx))-1 + (m.group(midx+1) == '0')
    t += ''.join(s[sl:l])
    sl = l
    while i < len(p) and p[i][0] != '@':
      if i+1 < len(p) and p[i+1][0] == '\\': line = p[i][:-1]; i += 2
      else: line = p[i]; i += 1
      if len(line) > 0:
        if line[0] == sign or line[0] == ' ': t += line[1:]
        sl += (line[0] != sign)
  t += ''.join(s[sl:])
  return t


def getSha(path):
    f = open(path).read()
    return githash.generic_hash(path, '100644', len(f)).hexdigest()


def updateFromZip():
    dp = platformtools.dialog_progress_bg('Kodi on Demand', 'Aggiornamento in corso...')
    dp.update(0)

    remotefilename = 'https://github.com/' + user + "/" + repo + "/archive/" + branch + ".zip"
    localfilename = xbmc.translatePath("special://home/addons/") + "plugin.video.kod.update.zip"
    logger.info("remotefilename=%s" % remotefilename)
    logger.info("localfilename=%s" % localfilename)

    import urllib
    urllib.urlretrieve(remotefilename, localfilename, lambda nb, bs, fs, url=remotefilename: _pbhook(nb, bs, fs, url, dp))

    # Lo descomprime
    logger.info("decompressione...")
    destpathname = xbmc.translatePath("special://home/addons/")
    logger.info("destpathname=%s" % destpathname)

    try:
        hash = fixZipGetHash(localfilename)
        unzipper = ziptools()
        unzipper.extract(localfilename, destpathname)
    except Exception as e:
        logger.info('Non sono riuscito ad estrarre il file zip')
        logger.info(e)
        return False

    dp.update(95)

    # puliamo tutto
    shutil.rmtree(addonDir)

    filetools.rename(destpathname + "addon-" + branch, addonDir)

    logger.info("Cancellando il file zip...")
    remove(localfilename)

    dp.update(100)
    return hash


# https://stackoverflow.com/questions/3083235/unzipping-file-results-in-badzipfile-file-is-not-a-zip-file
def fixZipGetHash(zipFile):
    f = io.FileIO(zipFile, 'r+b')
    data = f.read()
    pos = data.find(b'\x50\x4b\x05\x06')  # End of central directory signature
    hash = ''
    if pos > 0:
        f.seek(pos + 20)  # +20: see secion V.I in 'ZIP format' link above.
        hash = f.read()[2:]
        f.seek(pos + 20)
        f.truncate()
        f.write(
            b'\x00\x00')  # Zip file comment length: 0 byte length; tell zip applications to stop reading.
        f.seek(0)

    f.close()

    return str(hash)


def _pbhook(numblocks, blocksize, filesize, url, dp):
    try:
        percent = min((numblocks*blocksize*90)/filesize, 100)
        dp.update(percent)
    except:
        percent = 90
        dp.update(percent)


def remove(file):
    if os.path.isfile(file):
        removed = False
        while not removed:
            try:
                os.remove(file)
                removed = True
            except:
                logger.info('File ' + file + ' NON eliminato')
