const axios = require('axios');
const { genAuthCookies } = require('./utils');

const validateStatus = (status) => status < 500;

module.exports = {
  async getUser(session, signature = '', location = 'https://www.tradingview.com/', redirectCount = 0) {
    if (redirectCount > 5) {
      throw new Error('Too many redirects');
    }

    const { data, headers } = await axios.get(location, {
      headers: {
        cookie: genAuthCookies(session, signature),
      },
      maxRedirects: 0,
      validateStatus,
    });

    if (typeof data === 'string' && data.includes('auth_token')) {
      return {
        username: /"username":"(.*?)"/.exec(data)?.[1],
        authToken: /"auth_token":"(.*?)"/.exec(data)?.[1],
        session,
        signature,
      };
    }

    if (headers.location && headers.location !== location) {
      return this.getUser(session, signature, headers.location, redirectCount + 1);
    }

    throw new Error('Wrong or expired sessionid/signature');
  },
};