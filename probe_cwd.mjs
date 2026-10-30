console.log("cwd:", process.cwd());
console.log("dirname:", typeof __dirname !== "undefined" ? __dirname : "UNDEFINED");
console.log("argv:", JSON.stringify(process.argv));
const fs = require("node:fs");
console.log("ls cwd:", fs.readdirSync(".").slice(0,10));
