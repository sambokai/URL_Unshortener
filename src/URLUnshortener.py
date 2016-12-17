import configparser
import logging
import queue
import re
import threading
import time

import praw
import requests
from bs4 import BeautifulSoup

# TODO: add "About" section in README.md (learning python, first python project, cs student, etc..)

cfg_file = None
reddit = None
shorturl_services = None

# Queue of comments, populated by CommentScanner, to be filtered by CommentFilter
comments_to_filter = queue.Queue()
# Queue of comments, populated by CommentFilter, to be revealed and answered to by CommentRevealer
comments_to_reveal = queue.Queue()

# Configure logger
logger = logging.getLogger()
logfilename = str(time.strftime("%Y-%m-%d_%H-%M-%S") + '.logfile.log')
try:
    file_log_handler = logging.FileHandler("logfiles/" + logfilename)
except FileNotFoundError as e:
    print(e)
    print("Will only log to console.")

logger.addHandler(file_log_handler)

stderr_log_handler = logging.StreamHandler()
logger.addHandler(stderr_log_handler)

formatter = logging.Formatter('%(asctime)s: [%(levelname)s] %(message)s')
file_log_handler.setFormatter(formatter)
stderr_log_handler.setFormatter(formatter)

logger.setLevel(logging.INFO)

logging.debug("Initialized logger")


def main():
    """ TESTLINK: http://ow.ly/h4p230754Gt """
    logger.warning("\n\n\nProgram started.")
    read_config()
    connect_praw()

    comment_filter = CommentFilter()
    comment_scanner = CommentScanner()
    comment_revealer = CommentRevealer()

    filter_thread = threading.Thread(target=comment_filter.run_pushshift, args=())
    scan_thread = threading.Thread(target=comment_scanner.run_pushshift, args=())
    reveal_thread = threading.Thread(target=comment_revealer.run, args=())

    filter_thread.start()
    scan_thread.start()
    reveal_thread.start()


# First pass
class CommentScanner:
    def __init__(self):
        self.max_commentlength = int(cfg_file['urlunshortener']['max_commentlength'])
        self.SCAN_SUBREDDIT = cfg_file.get('urlunshortener', 'scan_subreddit')
        self.subs_to_scan = reddit.subreddit(self.SCAN_SUBREDDIT)
        self.firstpass_pattern_string = cfg_file['urlunshortener']['firstpass_url_regex_pattern']
        self.firstpass_regex = re.compile(self.firstpass_pattern_string)

        # Debug
        logger.info("First-Pass RegEx: " + self.firstpass_pattern_string)
        logger.info("Subreddits to scan: " + str(self.SCAN_SUBREDDIT))

    # in case pushshift stops working continue work on own implementation
    '''
    def run(self):
        for comment in self.subs_to_scan.stream.comments():
            body = comment.body
            if len(body) < self.max_commentlength:
                match = self.firstpass_regex.search(body)
                if match:
                    # print("\n\nMatch #", matchcounter, "   Total #", totalcounter,
                    #       "   URL: ", match.group(0))
                    comments_to_filter.put(comment)
    '''

    def run_pushshift(self):
        initial_timeout = 10  # initial timeout before fetching the first batch
        lastpage_timeout = 30  # timeout before restarting fetch, after having reached the most recent comment
        lastpage_url = None  # used to check if site has changed
        # fetch the latest 50 comments
        request = requests.get('https://apiv2.pushshift.io/reddit/comment/search')
        json = request.json()
        comments = json["data"]

        # use the latest comment's id (#50 out of 50) to save the next url. pushshifts after_id paramater allows us to
        # continue after the specified id. that way no comments are skipped
        initial_page_url = "http://apiv2.pushshift.io/reddit/comment/search/?sort=asc&limit=50&after_id=" + \
                           str(comments[0]['id'])
        next_page_url = initial_page_url
        # fixme: dont skip first 50 comments; so instead of waiting, use the time to process the first 50 comments
        # wait before next api request, if we don't wait there will be no "metadata" element.
        time.sleep(initial_timeout)
        logger.info("Start fetching comments...")
        # comment fetch loop
        while True:
            # request the comment-batch that comes after the initial batch
            request = requests.get(next_page_url)
            json = request.json()
            comments = json["data"]
            meta = json["metadata"]
            # use the next_page url to determine wether there is new data
            # if "next_page" key doesnt exist in 'metadata", it means that we are on the most current site
            if 'next_page' in str(meta) and lastpage_url != (meta['next_page']):
                # process the current batch of comments
                for rawcomment in comments:
                    body = rawcomment['body']
                    if len(body) < self.max_commentlength:
                        match = self.firstpass_regex.search(body)
                        if match:
                            # put relevant comments (containing urls) in queue for other thread to further process
                            comments_to_filter.put(rawcomment)
                # save on which page we are to check when new page arrives using the "next_page" link
                lastpage_url = meta['next_page']
                # use the "next_page" link to fetch the next batch of comments
                next_page_url = lastpage_url
                # wait before requesting the next batch
                time.sleep(1)
            else:
                logger.debug("Reached latest page. Wait " + str(lastpage_timeout) + " seconds.")
                time.sleep(lastpage_timeout)
            logger.debug(str(lastpage_url))


# Second pass
class CommentFilter:
    def __init__(self):
        self.secondpass_pattern_string = cfg_file['urlunshortener']['secondpass_url_regex_pattern']
        self.secondpass_regex = re.compile(self.secondpass_pattern_string)

        # Debug
        logger.info("Second-Pass RegEx: " + self.secondpass_pattern_string)

    # in case pushshift stops working continue work on own implementation here
    '''
    def run(self):
        while True:
            if comments_to_filter.not_empty:
                comment = comments_to_filter.get()
                print(comments_to_filter.qsize())
                match = self.secondpass_regex.search(comment.body)
                if match:
                    url = completeurl(match.group(0))
                    print("\nQueue size: ", comments_to_filter.qsize(), " URL: ",
                          url)
    '''

    def run_pushshift(self):
        while True:
            if comments_to_filter.not_empty:
                comment = comments_to_filter.get()
                match = self.secondpass_regex.search(comment['body'])
                if match:
                    url = completeurl(match.group(0))
                    if any(word in url for word in shorturl_services):
                        comments_to_reveal.put(comment)


# Third pass
class CommentRevealer:
    def __init__(self):
        self.thirdpass_pattern_string = cfg_file['urlunshortener']['thirdpass_url_regex_pattern']
        self.thirdpass_regex = re.compile(self.thirdpass_pattern_string)

        # Debug
        logger.info("Third-Pass RegEx: " + self.thirdpass_pattern_string)

    def run(self):  # TODO: only run this thread when comment is found & put in cmnts_to_reveal. dont run all the time
        while True:
            if comments_to_reveal.not_empty:
                comment = comments_to_reveal.get()
                self.checkforreveal(comment)

    def checkforreveal(self, comment):
        matches = self.thirdpass_regex.findall(comment['body'])
        if len(matches) != 0:  # i prefer the explicit check over the pythonic 'if not matches'.deal with it ;)
            foundurls = []
            try:
                for match in matches:
                    if any(word in match for word in shorturl_services):
                        shorturl = str(match)
                        unshortened = str(unshorten_url(match))
                        foundurls.append((shorturl, unshortened))
            except Exception as exception:
                logger.error(exception)
            finally:
                if len(foundurls) > 0:
                    # reply to comment
                    self.replytocomment(comment, foundurls)
                    # log
                    logtext = "Found comment containing " + str(len(foundurls)) + " short-url(s):"
                    for url in foundurls:
                        logtext += ("\nShort link: " + url[0] + " ; Unshortened link: " + url[1])
                    logtext += ("\nComment details: " + str(comment) + "\n")
                    logger.info(logtext)

    @staticmethod
    def replytocomment(comment, foundurls):
        """placeholder"""


def read_config():
    global cfg_file, shorturl_services
    cfg_file = configparser.ConfigParser()
    cfg_file.read('url-unshortener.cfg')

    shorturl_list_path = cfg_file.get('urlunshortener', 'shorturlserviceslist_path')

    # Read in list of shorturl-services as list-object.
    try:
        with open(shorturl_list_path) as f:
            shorturl_services = f.read().splitlines()
    except FileNotFoundError as e:
        logger.error(e)
        logger.error("Please check services-list file or specified path in configuration file (.cfg) and restart "
                     "URLUnshortener.")
        raise SystemExit(0)


def connect_praw():
    global cfg_file, reddit
    app_id = cfg_file['reddit']['app_id']
    app_secret = cfg_file['reddit']['app_secret']
    user_agent = cfg_file['reddit']['user_agent']
    reddit_account = cfg_file['reddit']['username']
    reddit_passwd = cfg_file['reddit']['password']

    # Start PRAW Reddit Session
    logger.info("Connecting to Reddit...")
    reddit = praw.Reddit(user_agent=user_agent, client_id=app_id, client_secret=app_secret, username=reddit_account,
                         password=reddit_passwd)
    logger.info("Connection successful. Reddit session started.")


def completeurl(url):
    if url.endswith(" "):
        url = url[:-1]
    if (not url.startswith('http://')) and (not url.startswith('https://')):
        return 'http://' + url
    else:
        return url


def unshorten_url(url):
    url = completeurl(url)
    resolved_url = ""
    maxattempts = 3
    # try again in n seconds
    timeout = 5
    for attempt in range(maxattempts):
        try:
            resolved_url = resolve_shorturl(url)
            break
        except Exception as exception:
            logging.error("Attempt #" + str(attempt + 1) + " out of " + str(maxattempts) + "max attempts failed. Error "
                                                                                           "message: "
                          + str(exception))
            if attempt + 1 == maxattempts:
                logging.error("All " + str(maxattempts) + "attempts have failed.")
                raise exception
            # wait [timeout] seconds before new attempt
            time.sleep(timeout)

    if url == resolved_url:
        raise Exception("URL is not shortened. URL: ", url)
    else:
        return resolved_url


def resolve_shorturl(url):
    url = completeurl(url)

    # get response (header) and disallow automatic redirect-following, since we want to control that ourselves.
    response = requests.head(url, allow_redirects=False)
    # if response code is a redirection (3xx)
    if 300 <= response.status_code <= 399:
        redirect_url = response.headers.get('Location')
        # Attempt to unshorten the redirect
        return resolve_shorturl(redirect_url)
    elif 200 <= response.status_code <= 299:
        # request the entire content (not just header) on "200" pages, in order to check for meta-refresh
        response = requests.get(url, allow_redirects=False)
        soup = BeautifulSoup(response.content, "html.parser")
        meta_refresh = soup.find("meta", attrs={"http-equiv": "Refresh"})
        # if html body contains a meta_refresh tag (which can be used to redirect to another url)
        if meta_refresh:
            wait, text = meta_refresh["content"].split(";")
            # if meta_refersh is indeed used to redirect and a url is provided
            if text.strip().lower().startwith("url="):
                meta_redirect_url = text[4:]
                # attempt to unshorten the meta_refresh url
                return resolve_shorturl(meta_redirect_url)
        else:
            return url
    else:
        raise Exception(str(
            response.status_code) + " HTTP Response. URL could not be unshortened. Is the link valid? (" + url + ")")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.error("KeyboardInterrupt")
        raise SystemExit(0)
