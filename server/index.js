// This file is part of switchbota (https://github.com/kendallgoto/switchbota/).
// Copyright (c) 2022 Kendall Goto.

// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, version 3.

// This program is distributed in the hope that it will be useful, but
// WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
// General Public License for more details.

// You should have received a copy of the GNU General Public License
// along with this program. If not, see <http://www.gnu.org/licenses/>.

const express = require('express');
const app = express();
const port = process.env.PORT || 80;
const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const { execFileSync } = require('child_process');
const dns = require('native-dns');
const async = require('async');
let LOCAL_IP = "0.0.0.0"
const PUBLIC_DNS = "8.8.8.8"

function startDns() {
	const dns_server = dns.createServer();
	let authority = { address: PUBLIC_DNS, port: 53, type: 'udp' };
	function dns_proxy(question, response, cb) {
		console.log('looking up ' + question.name);
		var request = dns.Request({
			question: question,
			server: authority,
			timeout: 1000
		});
		request.on('message', (err, msg) => {
			msg.answer.forEach(a => response.answer.push(a));
		});
		request.on('end', cb);
		request.send();
	}

	let entries = [{
		domain: ".*wohand\.com$",
		records: [{ type: "A", address: LOCAL_IP, ttl: 60 }]
	}];
	function handleDNSRequest(request, response) {
		console.log('request from', request.address.address, 'for', request.question[0].name);
		let f = []; // array of functions
		request.question.forEach(question => {
			let entry = entries.filter(r => new RegExp(r.domain, 'i').exec(question.name));
			if (entry.length) {
				entry[0].records.forEach(record => {
					record.name = question.name;
					record.ttl = record.ttl || 1800;
					response.answer.push(dns[record.type](record));
				});
			} else {
				f.push(cb => dns_proxy(question, response, cb));
			}
		});
		// do the proxying in parallel
		// when done, respond to the request by sending the response
		async.parallel(f, function () { response.send(); });
	}
	dns_server.on('request', handleDNSRequest);
	dns_server.on('listening', () => console.log('server listening on', dns_server.address()));
	dns_server.on('close', () => console.log('server closed', dns_server.address()));
	dns_server.on('error', (err, buff, req, res) => console.error(err.stack));
	dns_server.on('socketError', (err, socket) => console.error(err));
	dns_server.serve(53);
}

const args = process.argv.slice(2);
if (args.length) {
	LOCAL_IP = args[0]
	console.log("Starting with DNS server forwarding wohand.com to " + LOCAL_IP)
	startDns()
}
// You can temporarily set your router's DNS to the host this host's IP
// and it will correctly take care of wohand.com resolution.
const APP_PATH = path.join(__dirname, 'bin', 'app.bin'); // espressif/build/switchbota.bin from this repo
const PAYLOAD_PATH = path.join(__dirname, 'bin', 'payload.bin'); // ESPHome firmware.factory.bin
const WRITE_SIZE = 0x171000; // keep in step with espressif/main/include/config.h
const EVENT_HISTORY = 1000;

function fatal(message) {
	console.error(message);
	process.exit(1);
}

function readImage(file, hint) {
	if (!fs.existsSync(file)) {
		fatal(`Missing ${file}: ${hint}`);
	}
	const image = fs.readFileSync(file);
	// ESP image header: magic at byte 0, chip id at bytes 12-13 (5 is ESP32-C3)
	if (image.length < 24 || image[0] !== 0xE9 || image.readUInt16LE(12) !== 0x0005) {
		fatal(`${file} is not an ESP32-C3 image`);
	}
	return image;
}

function normalizeMac(mac) {
	const parts = String(mac).toLowerCase().split(/[:-]/);
	if (parts.length !== 6 || parts.some(p => !/^[0-9a-f]{1,2}$/.test(p))) {
		return null;
	}
	return parts.map(p => p.padStart(2, '0')).join(':');
}

function arpLookup(ip) {
	try {
		if (fs.existsSync('/proc/net/arp')) {
			const rows = fs.readFileSync('/proc/net/arp', 'utf8').split('\n').slice(1);
			for (const row of rows) {
				const cols = row.trim().split(/\s+/);
				if (cols[0] === ip && cols[3] !== '00:00:00:00:00:00') {
					return normalizeMac(cols[3]);
				}
			}
			return null;
		}
		const rows = execFileSync('arp', ['-a'], { encoding: 'utf8' }).split('\n');
		for (const row of rows) {
			const mac = row.match(/([0-9a-f]{1,2}[:-]){5}[0-9a-f]{1,2}/i);
			if (mac && row.split(/[\s()]+/).includes(ip)) {
				return normalizeMac(mac[0]);
			}
		}
	} catch (error) {
		console.log(`ARP lookup failed: ${error}`);
	}
	return null;
}

const events = [];
let lastSeq = 0;

// The flasher reports its own MAC in a header. The stock firmware does not, so stage 1
// relies on the ARP table, which only knows the plug if it is on this host's subnet.
function emit(event, req, extra) {
	const ip = (req.ip || '').replace(/^::ffff:/, '');
	const mac = normalizeMac(req.get('X-Device-MAC') || '') || arpLookup(ip);
	const entry = Object.assign({ seq: ++lastSeq, time: new Date().toISOString(), event, ip, mac, path: req.path }, extra);
	events.push(entry);
	if (events.length > EVENT_HISTORY) {
		events.shift();
	}
	console.log(JSON.stringify(entry));
}

(async function () {
	// Refuse to start with anything that would leave a plug erased and looping.
	const appBin = readImage(APP_PATH, 'build espressif/ and copy build/switchbota.bin here as app.bin');
	if (!appBin.includes('payload.md5')) {
		fatal(`${APP_PATH} never asks for payload.md5, so it is an upstream build that only accepts Tasmota`);
	}

	const payload = readImage(PAYLOAD_PATH, 'copy the ESPHome firmware.factory.bin here as payload.bin');
	if (payload.length > WRITE_SIZE) {
		fatal(`${PAYLOAD_PATH} is ${payload.length} bytes, the flasher can only write ${WRITE_SIZE}`);
	}
	if (payload.length < 0x8002 || payload.readUInt16LE(0x8000) !== 0x50AA) {
		fatal(`${PAYLOAD_PATH} has no partition table at 0x8000, it must be the factory image, not the OTA one`);
	}
	if (payload[2] !== 2 && payload[2] !== 3) {
		fatal(`${PAYLOAD_PATH} bootloader flash mode is ${payload[2]}, expected 2 (dio) or 3 (dout)`);
	}
	// payload.md5 and payload.bin are both served from this one in-memory copy, so they cannot disagree
	const payloadMd5 = crypto.createHash('md5').update(payload).digest('hex');
	console.log(`app.bin: ${appBin.length} bytes`);
	console.log(`payload.bin: ${payload.length} bytes, md5 ${payloadMd5}, ${WRITE_SIZE - payload.length} bytes of headroom`);

	app.get('/events', (req, res) => {
		const since = parseInt(req.query.since, 10) || 0;
		res.json({ seq: lastSeq, events: events.filter(e => e.seq > since) });
	});
	app.get('/payload.md5', (req, res) => {
		emit('payload_md5', req);
		res.type('text/plain').send(`${payloadMd5} ${payload.length}\n`);
	});
	app.get('/payload.bin', (req, res) => {
		const started = Date.now();
		emit('payload_start', req, { size: payload.length });
		// With "Connection: close" the socket only closes once the plug hangs up, so this fires
		// when the plug is finished with the download rather than when the kernel has buffered
		// it. It cannot tell a good download from one the plug gave up on: a plug that failed
		// comes back for payload.md5 a few seconds later, a plug that verified reboots.
		req.socket.once('close', (hadError) => {
			const ok = res.writableFinished && !hadError;
			emit(ok ? 'payload_done' : 'payload_aborted', req, { seconds: Math.round((Date.now() - started) / 1000) });
		});
		res.set({ 'Content-Type': 'application/octet-stream', 'Content-Length': payload.length, 'Connection': 'close' });
		res.end(payload);
	});
	app.get('*', (req, res) => {
		if (req.path === '/app.bin') {
			emit('flip', req);
		} else if (req.path.endsWith('.bin')) {
			emit('stage1', req);
		} else {
			console.log(`${req.ip} - ${req.url}`);
		}
		res.setHeader('Content-Type', 'application/octet-stream');
		res.sendFile(APP_PATH);
	});

	app.listen(port, () => {
		console.log(`Server listening on port ${port}`);
	})
})();
