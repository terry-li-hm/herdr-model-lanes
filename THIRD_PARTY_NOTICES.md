# CLIProxyAPI adapter attribution

`proxy_capacity.py` adapts the account discovery and management `api-call`
protocol patterns from [Zigerry/cliproxy-usage](https://github.com/Zigerry/cliproxy-usage),
revision `3f4cf3e58cb8ef28ffc497b4e1efbee79dc57911`, specifically
`src/usage.ts` and `src/parsers.ts`. The implementation here uses Python's standard
library and adds launch eligibility, model restrictions and bounded caching.
The upstream project is MIT licensed. Its notice follows. The rest of this
repository retains its Apache-2.0 license.

MIT License

Copyright (c) 2026 Villoh
Copyright (c) 2026 Zigerry

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
