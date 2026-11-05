// node docs/site/check.mjs: fails if the docs' renderer, search or links break, or the site starts loading something external.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const read=f=>readFileSync(new URL(f,import.meta.url),"utf8");
const docs=read("docs.html"),home=read("index.html"),css=read("site.css");

// docs.html's own script (the one <script> with no attributes), with no document, so it only defines its functions.
const ctx={};
vm.runInNewContext(docs.match(/<script>([\s\S]*?)<\/script>/)[1],ctx);
const {md,sections,searchPages}=ctx;

// Nothing written in a page becomes markup, and a link can only go somewhere safe.
const bad=md('<img src=x onerror=alert(1)> `<b>` [x](javascript:alert(1)) [y](#ok)',"p").html;
assert(!bad.includes("<img")&&!bad.includes("<b>")&&!bad.includes("javascript:"),bad);
assert(bad.includes('<a href="#ok">y</a>')&&bad.includes("<code>&lt;b&gt;</code>"),bad);

// Headings, fences, lists, tables and a pipe inside a table's code.
const page=md('# T\n\nIntro\ntext\n\n## A `b` c\n\n- one\n- two\n\n1. x\n\n```\n# not a heading\n```\n\n| h |\n| --- |\n| `a\\|b` |\n\n> note',"p");
assert.equal(page.title,"T");
assert.equal(page.heads[0].id,"a-b-c");
assert(page.html.includes("<p>Intro text</p>")&&page.html.includes("<ul><li>one</li><li>two</li></ul>")&&page.html.includes("<ol><li>x</li></ol>"));
assert(page.html.includes("# not a heading")&&!page.html.includes("<h1 tabindex=\"-1\">not a heading"));
assert(page.html.includes("<td><code>a|b</code></td>")&&page.html.includes('<div class="note"><p>note</p></div>'));

// Every page, and every link to one.
const pages=[...docs.matchAll(/<script type="text\/markdown" data-slug="([^"]+)" data-group="([^"]+)">\n([\s\S]*?)<\/script>/g)].map(m=>({slug:m[1],group:m[2],src:m[3].trim(),sections:sections(m[3]),...md(m[3].trim(),m[1])}));
assert(pages.length>=13,"pages: "+pages.length);
const known=new Set(pages.flatMap(p=>[p.slug,...p.heads.map(h=>p.slug+"/"+h.id)]));
const links=[...pages.flatMap(p=>[...p.src.matchAll(/\]\(#([^)]+)\)/g)].map(m=>m[1])),...[...home.matchAll(/href="docs\.html#([^"]+)"/g)].map(m=>m[1])];
for(const l of links)assert(known.has(l),"dead link: #"+l);
for(const p of pages)assert(p.title&&p.heads.length,p.slug+" needs an H1 and an H2");

// Search: all words must match, a title comes first, and nothing found is an empty list.
assert.equal(searchPages(pages,"write a plugin")[0].page.slug,"write-plugin");
assert(searchPages(pages,"relay forge").some(h=>h.section.id==="the-relay"));
assert.equal(searchPages(pages,"nothing-has-this-word").length,0);
assert.equal(searchPages(pages,"").length,pages.length);

// Plain static files: nothing loaded from elsewhere, no em dashes, no internal host.
const site=docs+home+css;
assert(!/<(script|img|iframe|link|source)\b[^>]*\b(src|href)=["']?https?:/i.test(site),"external load");
assert(!/url\(["']?https?:/i.test(css),"external font or image");
assert(!site.includes("\u2014"),"em dash");
assert(!/\.ts\.net/.test(site),"internal host");

console.log("ok:",pages.length,"pages,",links.length,"links");
