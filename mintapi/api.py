import datetime
import json
import random
import requests
import ssl
import time
import xmltodict

from requests.adapters import HTTPAdapter
from requests.packages.urllib3.poolmanager import PoolManager

DATE_FIELDS = [
    'addAccountDate',
    'closeDate',
    'fiLastUpdated',
    'lastUpdated',
]

class MintHTTPSAdapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, **kwargs):
        self.poolmanager = PoolManager(num_pools=connections, maxsize=maxsize, **kwargs)

class Mint:
    headers = {"accept": "application/json"}
    request_id = 42 # magic number? random number?
    session = None
    token = None

    def __init__(self, email=None, password=None):
        if email and password:
            self.login_and_get_token(email, password)
            
    @classmethod
    def create(_, email, password): # {{{
        mint = Mint()
        mint.login_and_get_token(email, password)
        return mint
    # }}}

    @classmethod
    def get_rnd(_): # {{{
        return str(int(time.mktime(datetime.datetime.now().timetuple()))) + str(random.randrange(999)).zfill(3)
    # }}}

    @classmethod
    def parse_float(_, string): # {{{
        for bad_char in ['$', ',', '%']:
            string = string.replace(bad_char, '')

        try:
            return float(string)
        except ValueError:
            return None
    # }}}

    def login_and_get_token(self, email, password): # {{{
        # 0: Check to see if we're already logged in.
        if(self.token != None):
            return

        # 1: Login.
        self.session = requests.Session()
        self.session.mount('https://', MintHTTPSAdapter())
        if self.session.get("https://wwws.mint.com/login.event?task=L").status_code != requests.codes.ok:
            raise Exception("Failed to load Mint login page")

        data = {'username' : email}
        response = self.session.post('https://wwws.mint.com/getUserPod.xevent', data = data, headers = self.headers).text

        data = {"username": email, "password": password, "task": "L", "browser": "firefox", "browserVersion": "27", "os": "linux"}
        response = self.session.post("https://wwws.mint.com/loginUserSubmit.xevent", data=data, headers=self.headers).text
        if "token" not in response:
            raise Exception("Mint.com login failed[1]")

        response = json.loads(response)
        if not response["sUser"]["token"]:
            raise Exception("Mint.com login failed[2]")

        # 2: Grab token.
        self.token = response["sUser"]["token"]
    # }}}

    def get_accounts(self, get_detail = False): # {{{
        # Issue service request.
        req_id = str(self.request_id)
        data = {"input": json.dumps([
            {"args": {
                "types": [
                    "BANK", 
                    "CREDIT", 
                    "INVESTMENT", 
                    "LOAN", 
                    "MORTGAGE", 
                    "OTHER_PROPERTY", 
                    "REAL_ESTATE", 
                    "VEHICLE", 
                    "UNCLASSIFIED"
                ]
            }, 
            "id": req_id, 
            "service": "MintAccountService", 
            "task": "getAccountsSorted"
            #"task": "getAccountsSortedByBalanceDescending"
            }
        ])}
        response = self.session.post("https://wwws.mint.com/bundledServiceController.xevent?legacy=false&token="+self.token, data=data, headers=self.headers).text
        self.request_id = self.request_id + 1
        if req_id not in response:
            raise Exception("Could not parse account data: " + response)

        # Parse the request
        response = json.loads(response)
        accounts = response["response"][req_id]["response"]

        # Return datetime objects for dates
        for account in accounts:
            for df in DATE_FIELDS:
                if df in account:
                    # Convert from javascript timestamp to unix timestamp
                    # http://stackoverflow.com/a/9744811/5026
                    try:
                        ts = account[df] / 1e3
                    except TypeError:
                        # returned data is not a number, don't parse
                        continue
                    account[df + 'InDate'] = datetime.datetime.fromtimestamp(ts)
        if(get_detail):
            accounts = self.populate_extended_account_detail(accounts)
        return accounts
    # }}}

    def populate_extended_account_detail(self, accounts): # {{{
        # I can't find any way to retrieve this information other than by
        # doing this stupid one-call-per-account to listTransactions.xevent
        # and parsing the HTML snippet :(
        for account in accounts:
            headers = self.headers
            headers['Referer'] = 'https://wwws.mint.com/transaction.event?accountId=' + str(account['id'])
            response = json.loads(self.session.get(
                'https://wwws.mint.com/listTransaction.xevent?accountId=' + str(account['id']) + '&queryNew=&offset=0&comparableType=8&acctChanged=T&rnd=' + Mint.get_rnd(),
                headers = headers
            ).text)
            xml = '<div>' + response['accountHeader'] + '</div>'
            xml = xml.replace('&#8211;', '-')
            xml = xmltodict.parse(xml)

            account['availableMoney'] = None
            account['totalFees'] = None
            account['totalCredit'] = None
            account['nextPaymentAmount'] = None
            account['nextPaymentDate'] = None

            xml = xml['div']['div'][1]['table']
            if(not 'tbody' in xml):
                continue
            xml = xml['tbody']
            table_type = xml['@id']
            xml = xml['tr'][1]['td']

            if(table_type == 'account-table-bank'):
                account['availableMoney'] = Mint.parse_float(xml[1]['#text'])
                account['totalFees'] = Mint.parse_float(xml[3]['a']['#text'])
                if(account['interestRate'] == None):
                    account['interestRate'] = Mint.parse_float(xml[2]['#text']) / 100.0
            elif(table_type == 'account-table-credit'):
                account['availableMoney'] = Mint.parse_float(xml[1]['#text'])
                account['totalCredit'] = Mint.parse_float(xml[2]['#text'])
                account['totalFees'] = Mint.parse_float(xml[4]['a']['#text'])
                if(account['interestRate'] == None):
                    account['interestRate'] = Mint.parse_float(xml[3]['#text']) / 100.0
            elif(table_type == 'account-table-loan'):
                account['nextPaymentAmount'] = Mint.parse_float(xml[1]['#text'])
                account['nextPaymentDate'] = xml[2]['#text']
            elif(table_type == 'account-type-investment'):
                account['totalFees'] = Mint.parse_float(xml[2]['a']['#text'])

        return accounts
    # }}}

    def get_categories(self): # {{{
        # Get category metadata.
        req_id = str(self.request_id)
        data = {
            'input': json.dumps([{
                'args': {
                    'excludedCategories' : [],
                    'sortByPrecedence' : False,
                    'categoryTypeFilter' : 'FREE'
                },
                'id': req_id,
                'service': 'MintCategoryService',
                'task': 'getCategoryTreeDto2'
            }])
        }
        response = self.session.post('https://wwws.mint.com/bundledServiceController.xevent?legacy=false&token=' + self.token, data = data, headers = self.headers).text
        self.request_id = self.request_id + 1
        if(req_id not in response):
            raise Exception('Could not parse category data: "' + response + '"')
        response = json.loads(response)
        response = response['response'][req_id]['response']

        # Build category list 
        categories = {}
        for category in response['allCategories']:
            if(category['parentId'] == 0):
                continue
            categories[category['id']] = category

        return categories
    # }}}

    def get_budgets(self): # {{{
        # Get categories
        categories = self.get_categories()

        # Issue request for budget utilization
        today = datetime.date.today()
        this_month = datetime.date(today.year, today.month, 1)
        last_year = this_month - datetime.timedelta(days = 330)
        this_month = str(this_month.month).zfill(2) + '/01/' + str(this_month.year)
        last_year = str(last_year.month).zfill(2) + '/01/' + str(last_year.year)
        response = json.loads(self.session.get(
            'https://wwws.mint.com/getBudget.xevent?startDate=' + last_year + '&endDate=' + this_month + '&rnd=' + Mint.get_rnd(),
            headers = self.headers
        ).text)

        # Make the skeleton return structure
        budgets = {
            'income' : response['data']['income'][str(max(map(int, response['data']['income'].keys())))]['bu'],
            'spend' : response['data']['spending'][str(max(map(int, response['data']['income'].keys())))]['bu']
        }

        # Fill in the return structure
        for direction in budgets.keys():
            for budget in budgets[direction]:
                budget['cat'] = categories[budget['cat']]

        return budgets
    # }}}

    def initiate_account_refresh(self): # {{{
        # Submit refresh request.
        data = {
            'token' : self.token
        }
        response = self.session.post('https://wwws.mint.com/refreshFILogins.xevent', data = data, headers = self.headers)
    # }}}

def get_accounts(email, password, get_detail = False):
    mint = Mint.create(email, password)
    return mint.get_accounts(get_detail = get_detail)

def make_accounts_presentable(accounts):
    for account in accounts:
        for k, v in account.items():
            if isinstance(v, datetime.datetime):
                account[k] = repr(v)
    return accounts

def print_accounts(accounts):
    print(json.dumps(make_accounts_presentable(accounts), indent = 2))

def get_budgets(email, password):
    mint = Mint.create(email, password)
    return mint.get_budgets()

def initiate_account_refresh(email, password):
    mint = Mint.create(email, password)
    return mint.initiate_account_refresh()

def main():
    import getpass
    import optparse
    import sys

    # Parse command-line arguments {{{
    cmdline = optparse.OptionParser(usage = 'usage: %prog [options] email password')
    cmdline.add_option('--accounts', action = 'store_true', dest = 'accounts', default = False, help = 'Retrieve account information (default if nothing else is specified)')
    cmdline.add_option('--budgets', action = 'store_true', dest = 'budgets', default = False, help = 'Retrieve budget information')
    cmdline.add_option('--extended-accounts', action = 'store_true', dest = 'accounts_ext', default = False, help = 'Retrieve extended account information (slower, implies --accounts)')

    (options, args) = cmdline.parse_args()
    
    # Handle Python 3's raw_input change.
    try:
        input = raw_input
    except NameError:
        pass

    if(len(args) >= 2):
        (email, password) = args[:2]
    else:
        email = input("Mint email: ")
        password = getpass.getpass("Password: ")

    if(options.accounts_ext):
        options.accounts = True

    if(not (options.accounts or options.budgets)):
        options.accounts = True
    # }}}

    mint = Mint.create(email, password)
    
    data = None
    if(options.accounts and options.budgets):
        try:
            accounts = make_accounts_presentable(mint.get_accounts(get_detail = options.accounts_ext))
        except:
            accounts = None

        try:
            budgets = mint.get_budgets()
        except:
            budgets = None

        data = {'accounts' : accounts, 'budgets' : budgets}
    elif(options.budgets):
        try:
            data = mint.get_budgets()
        except:
            data = None
    elif(options.accounts):
        try:
            data = make_accounts_presentable(mint.get_accounts(get_detail = options.accounts_ext))
        except:
            data = None
    
    print(json.dumps(data, indent = 2))

if __name__ == "__main__":
    main()
