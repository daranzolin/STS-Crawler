"""This unit handles reading in data from the slayTheSpire wiki,
    creating a string comparison tool,
    reading posts from the slay the spire subreddit
    scanning the post titles for item mentions and replying with data
"""
import requests
import praw
import prawcore
import datetime
import os
import traceback

from STSTypes import *
from time import sleep
from bs4 import BeautifulSoup as soup
from urllib3.exceptions import InsecureRequestWarning
from strsimpy.jaro_winkler import JaroWinkler

sts_descr_site = 'http://127.0.0.1:5000/'
describe_path = 'describe'
update_path = 'update'

def log(text):
    """helper to log to file and print at the same time"""
    with open('sts_crawler.log', 'a', encoding='utf-8') as logger:
        log_text = text.replace('\n', '\n\t')
        logger.write(f'\n{str(datetime.datetime.utcnow())}: {log_text}')
    print(text)


class STSWikiReader:
    """Reads data from website, creates a lookup map of item names, and does
        soft string matching to find possible mentions of the item parsed
    """
    strcmp = JaroWinkler()

    def __init__(self, name, links, ignore_list, parse_names):
        self.last_update = datetime.datetime.utcnow()
        self.name = name
        self.links = links
        self.ignore_list = ignore_list
        self.parse_names = parse_names
        self.base_set = set()
        self.real_names = set()
        self.fake_name_map = dict()
        self.cur = None
        self.max_name_word_cnt = 0
        self.max_match = 0
        self.FORCE_IGNORE_NAME = '~~FORCE~IGNORE~~'

        self.update_info()

    def format_name(self, name):
        """Used to get a clean, uniform name with pesky characters removed"""
        return self._rm_double_space(self._rm_symbol(self._rm_squote(
            self._rm_hyph(name.lower()))))

    def _rm_symbol(self, name):
        """removes odd characters that should never be in a obj name"""
        return name.replace('?', ' ').replace(',', ' ').replace('.', ' ') \
            .replace('!', ' ').replace('(', ' ').replace(')', ' ') \
            .replace(':', ' ').replace('"', ' ').replace('+', ' ') \
            .replace('[', ' ').replace(']', ' ')

    def _rm_squote(self, name):
        """removes single quotes"""
        return name.replace("'", '').replace('’', '')

    def _lower(self, name):
        """exists to pass along to alternative names func"""
        return name.lower()

    def _rm_hyph(self, name):
        """swaps typical joining characters with spaces"""
        return name.replace('-', ' ').replace('_', ' ')

    def _rm_beta(self, name):
        """removes beta tag (possible error from wiki)"""
        return name.replace('_beta', '').replace('_Beta', '') \
            .replace('Beta', '').replace('beta', '')

    def _append_s(self, name):
        """makes things plural
            (simple method prone to error, but will do for now)
        """
        return f'{name}s'

    def _rm_double_space(self, name):
        while '  ' in name:
            pos = name.find('  ')
            name = name[:pos] + name[pos+1:]
        return name

    def _rm_article_at_start(self, name):
        articles = ['the', 'a', 'an']
        test_name = name.lower()
        for article in articles:
            if test_name.startswith(article + ' '):
                return name[len(article) + 1:]
        return name

    def _gen_alternative_names(self, name):
        """creates a massive list of possible mistypes for a
            specific name, used as an aid for matching user input
        """
        names = set()
        actions = [self._rm_symbol, self._rm_squote, self._lower,
                   self._rm_article_at_start, self._rm_hyph, self._rm_beta,
                   self._append_s]
        # Weird edge case for beta tag on wiki vs beta the card
        if name.lower().strip() == 'beta':
            actions.remove(self._rm_beta)

        for outer in range(len(actions)):
            temp_name = name
            for inner in range(len(actions) - outer):
                temp_name = self._rm_double_space(
                    actions[outer+inner](temp_name))
                names.add(temp_name)
        return list(names)

    def update_info(self):
        """goes to the web and finds information provided by the links"""
        log(f'Updating {self.name}s...')
        seen_list = set()

        # fetch data from links and update object with most recent info
        for link in self.links:
            res = requests.get(link, verify=False)
            for cur_name in self.parse_names(
                    soup(res.text, features="html.parser")):
                if cur_name.lower() in self.ignore_list:
                    continue
                seen_list.add(cur_name)
                # if we haven't seen it before, add it to our look up list.
                if (cur_name not in self.base_set) \
                        and (not cur_name.startswith('Category:')):
                    self.base_set.add(cur_name)
                    self.real_names.add(cur_name)
                    self.fake_name_map[cur_name] = cur_name
                    self.max_name_word_cnt = max(self.max_name_word_cnt,
                                                 len(cur_name.split(' ')))

                    for new_name in self._gen_alternative_names(cur_name):
                        if new_name.strip():
                            self.base_set.add(new_name)
                            self.fake_name_map[new_name] = cur_name

        # handle deleted data from wiki
        recalc_max_name_word_cnt = False
        for cur_name in self.real_names - seen_list:
            for new_name in self._gen_alternative_names(cur_name):
                self.base_set.remove(new_name)
                del self.fake_name_map[new_name]

            if not recalc_max_name_word_cnt \
                    and self.max_name_word_cnt == len(cur_name.split(' ')):
                recalc_max_name_word_cnt = True
            self.base_set.remove(cur_name)
            self.real_names.remove(cur_name)
            del self.fake_name_map[cur_name]

        if recalc_max_name_word_cnt:
            self.max_name_word_cnt = 0
            for cur_name in self.real_names:
                self.max_name_word_cnt = max(self.max_name_word_cnt,
                                             len(cur_name.split(' ')))

        # finalize update
        self.last_update = datetime.datetime.utcnow()
        log(f'Found {len(self.real_names)} {self.name}s')

    def check_if_similar(self, name):
        """uses similarity check to see if the passed in name may match
            any of our found or generated names
        """
        name = self.format_name(name)
        split_name = name.split(' ')
        word_thresh = 0.9**len(split_name)
        self.max_match = 0
        self.cur = None
        for item_name in self.base_set:
            split_item_name = item_name.split(' ')
            if len(split_name) == len(split_item_name):
                word_check = 1
                for i in range(len(split_name)):
                    word_check *= self.strcmp.similarity(
                        split_name[i], split_item_name[i])
                    word_check *= self.strcmp.similarity(
                        split_name[i][::-1], split_item_name[i][::-1])

                if word_check > self.max_match:
                    self.max_match = word_check
                    if word_check >= word_thresh:
                        self.cur = self.fake_name_map[item_name]
        return self.cur is not None

    def check_if_exists(self, name, update=True):
        """Used to check if a name is a perfect match for any found
            names or is close enough to call a match
        """
        if update and datetime.datetime.utcnow() - self.last_update \
                > datetime.timedelta(days=15):
            self.update_info()

        if name.lower() in self.ignore_list:
            self.cur = self.FORCE_IGNORE_NAME
            self.max_match = 1
            return True

        res = name in self.real_names
        if res:
            self.cur = name
            self.max_match = 1
        elif name in self.fake_name_map.keys():
            self.cur = self.fake_name_map[name]
            self.max_match = 1
            res = True
        else:
            res = self.check_if_similar(name)
        return res


class RedditBot:
    last_update = None

    def __init__(self, readers):
        self.REDDIT = self.login()
        self.SUBREDDIT = self.REDDIT.subreddit('slaythespire')
        self.readers = readers
        self.NEW_LINE = '\n\n'
        self.FIRST_REPLY_TEMPLATE = 'I am {:0.1f}% confident you mentioned ' \
                                    + '{} in your post.'
        self.REPLY_TEMPLATE = 'I am also {:0.1f}% confident you mentioned {}.'

        self.END_TEXT = 'Let me look up what those do.' + \
                        self.NEW_LINE + '-'*50 + \
                        self.NEW_LINE + "I am a bot response, but " + \
                        "I am using my creator's account. " + \
                        'Please reply to me if I got something wrong ' + \
                        'so he can fix it.' + self.NEW_LINE + \
                        '[Source Code](https://github.com/TrippW/STS-Crawler)'

    def login(self):
        """
        log in to reddit
        uses a praw.ini file to hold sensitive information
        """
        return praw.Reddit(redirect_uri='http://localhost:8080',
                           user_agent='STS Scraper by /u/devTripp')

    def start(self):
        """starts the bot, runs forever"""
        self.last_update = datetime.datetime.utcnow()
        while True:
            log('Starting up...')
            try:
                for post in self.SUBREDDIT.stream.submissions():
                    if datetime.datetime.utcnow() - self.last_update \
                            >= datetime.timedelta(days=1):
                        requests.get(sts_descr_site+update_path)
                        self.update_ignore_files()

                    self.process_submission(post)

            except Exception as e:
                log(str(e))
                traceback.print_stack(e)
                sleep(60)

    def update_ignore_files(self):
        """finds ignore files for our reader and pulls data into the readers.
            Used to update ignored strings during runtime
        """
        for reader in self.readers:
            f_ignore_name = f'{reader.name}.ignore'
            f_links_name = f'{reader.name}.link'
            if os.path.exists(f_ignore_name):
                with open(f_ignore_name, 'r') as f:
                    reader.ignore_list = [k.strip().lower()
                                          for k in f.readlines()]
                with open(f_links_name, 'r') as f:
                    reader.links = [k.strip() for k in f.readlines()]
            else:
                reader.ignore_list = []
                reader.links = []
            reader.last_update = datetime.datetime.utcnow()

        self.last_update = datetime.datetime.utcnow()

    def check_all_word_combos(self, title, on_true):
        """breaks the sentence/title into words/groups of words, and tries
            to match it with data in a reader
        """

        mentions = dict()
        for reader in self.readers:
            if datetime.datetime.utcnow() - reader.last_update \
                    > datetime.timedelta(days=15):
                reader.update_info()

        skip = 0
        words = list(filter(None, reader.format_name(
            title.replace('/', ' ').strip()).split(' ')))
        for word_pos in range(len(words)):
            if skip:
                skip -= 1
                continue
            cur = None
            best = 0
            match_type = None
            match_len = 0
            for offset in range(reader.max_name_word_cnt, 0, -1):
                if word_pos + offset > len(words):
                    continue
                phrase = ' '.join(words[word_pos:word_pos+offset])
                for reader in self.readers:
                    if reader.check_if_exists(phrase):
                        if reader.max_match > best:
                            cur = reader.cur
                            best = reader.max_match
                            match_type = reader.name
                            match_len = offset
                            if best == 1:
                                break
                if best == 1:
                    break
            if cur is not None:
                if cur != reader.FORCE_IGNORE_NAME:
                    if not mentions:
                        log(title)
                    print('{} Mention: {} | {:0.2f}'.format(
                        match_type, cur, best))
                    if cur in mentions.keys():
                        mentions[cur] = max(best*100, mentions[cur])
                    else:
                        mentions[cur] = best*100
                skip = match_len-1

        if mentions:
            on_true(mentions)
        return len(mentions) > 0

    def post_reply(self, items):
        """formats and posts the data to reddit"""
        reply = ""
        grouped_items = dict()
        abo = ['Alpha', 'Beta', 'Omega']
        names = []
        # group by percent
        for key in items:
            k = int(items[key]*10)
            if key in abo:
                abo.remove(key)
            if k not in grouped_items.keys():
                grouped_items[k] = [key]
            else:
                grouped_items[k].append(key)
        template = self.FIRST_REPLY_TEMPLATE
        for key in sorted(grouped_items, reverse=True):
            values = grouped_items[key]
            names.extend(values)
            if len(values) == 1:
                reply += template.format(key/10, f'{values[0]}') + \
                         self.NEW_LINE
            else:
                item_list = ', '.join([f'{k}' for k in values[:-1]])
                # oxford comma
                if len(values) != 2:
                    item_list += ','
                item_list += ' and ' + f'{values[-1]}'
                reply += template.format(key/10, item_list) + self.NEW_LINE
            template = self.REPLY_TEMPLATE

        if len(abo) % 3 != 0:
            names.append(abo[0])
            reply += f'You may also be interested in {abo[0]}'
            if len(abo) == 2:
                names.append(abo[1])
                reply += f' and {abo[1]}'
            reply += '.' + self.NEW_LINE

        log(reply)

        # ## DEBUG TEXT# ##
        global time_at_run
        log(f'last time sts_relics was run: {time_at_run}')
        log(f'last time ignores were updated: {self.last_update}')
        for reader in self.readers:
            log(f'last time {reader.name} reader was updated:' +
                f' {reader.last_update}')
        # ## END DEBUG# ##
        reply += self.END_TEXT
        try:
            res = requests.post(sts_descr_site+describe_path, json={'names':names})
        except requests.exceptions.HTTPError as e:
            log('Unable to get name descriptions')
            log(e.response.text)
            raise
        entries = [WikiEntry(**entry) for entry in res.json()['entries']]        

        cur_post = self.post.reply(reply)

        reply = ''
        reply_cnt = 0
        for entry in entries:
            reply_cnt += 1
            reply += entry.descr() + '\n\n'
            if reply_cnt == 10:
                log('posting next reply')
                cur_post.reply(reply)
                reply_cnt = 0

        if reply_cnt != 0:
            log('posting next reply')
            cur_post.reply(reply)
    def process_submission(self, post):
        """handles input of new posts to the subreddit"""
        global checked_ids
        title = post.title
        self.post = post

        title = str(title).encode('utf-8', errors='ignore').decode('utf-8')
        if (post.id not in checked_ids) and 'Daily Discussion' not in title:
            print(f'checking {post.id}')
            self.check_all_word_combos(title, self.post_reply)
            checked_ids.append(post.id)
            with open('checked.txt', 'a') as f:
                f.write('\n' + post.id)


checked_ids = []
can_post = True
time_at_run = datetime.datetime.utcnow()
bs_text = None

if __name__ == '__main__':
    def relic_parse(page_soup):
        return [a.text for a in page_soup.find_all(
            class_='category-page__member-link')]

    def card_parse(page_soup):
        return [row.find_next('a').text for row in
                page_soup.find('table').find_all('tr')]

    def get_data(filename):
        if os.path.exists(filename):
            with open(filename, 'r') as f:
                return [k.strip() for k in f.readlines()]

    # Read from files
    checked_ids = get_data('checked.txt')
    RELIC_IGNORE = [i.lower() for i in get_data('relic.ignore')]
    RELIC_LINKS = get_data('relic.link')
    CARD_IGNORE = [i.lower() for i in get_data('card.ignore')]
    CARD_LINKS = get_data('card.link')

    # Setup and run bot
    requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)
    RelicReader = STSWikiReader('relic',
                                RELIC_LINKS,
                                RELIC_IGNORE,
                                relic_parse)
    CardReader = STSWikiReader('card',
                               CARD_LINKS,
                               CARD_IGNORE,
                               card_parse)
    redditbot = RedditBot([RelicReader, CardReader])
    redditbot.start()

    # FOR TESTING
    if False:
        class tempPost:
            def __init__(self, title, _id):
                self.title = title
                self.id = _id
        redditbot.process_submission(tempPost(
           'Insane courier combo with "membership card!"', '4'))
