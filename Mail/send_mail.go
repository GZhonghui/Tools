// Send mail using the SMTP settings in config.json.
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"mime"
	"mime/multipart"
	"net"
	"net/mail"
	"net/smtp"
	"net/textproto"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

type config struct {
	Host, Security, Username, Password string
	Port                               int
}

func mailbox(s string) bool {
	addr, err := mail.ParseAddress(s)
	return err == nil && addr.Name == "" && addr.Address == s &&
		strings.IndexFunc(s, func(r rune) bool { return r > 127 || unicode.IsControl(r) }) == -1
}

func loadConfig() (config, error) {
	var cfg config
	exe, err := os.Executable()
	if err != nil {
		return cfg, err
	}
	if resolved, err := filepath.EvalSymlinks(exe); err == nil {
		exe = resolved
	}
	path := filepath.Join(filepath.Dir(exe), "config.json")
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		// go run places the executable outside the source directory.
		if _, source, _, ok := runtime.Caller(0); ok && filepath.IsAbs(source) {
			sourcePath := filepath.Join(filepath.Dir(source), "config.json")
			data, err = os.ReadFile(sourcePath)
			if os.IsNotExist(err) {
				return cfg, fmt.Errorf("请在程序或源文件目录创建 config.json (%s, %s)", path, sourcePath)
			}
			path = sourcePath
		}
	}
	if err != nil {
		return cfg, err
	}
	if err := json.Unmarshal(data, &cfg); err != nil {
		return cfg, fmt.Errorf("%s 格式错误: %w", path, err)
	}
	if cfg.Host == "" || strings.TrimSpace(cfg.Host) != cfg.Host ||
		strings.IndexFunc(cfg.Host, unicode.IsControl) >= 0 || cfg.Port < 1 || cfg.Port > 65535 ||
		!mailbox(cfg.Username) || cfg.Password == "" {
		return cfg, fmt.Errorf("请检查 %s 中的 host、port、username (完整邮箱地址) 和 password", path)
	}
	if cfg.Security != "ssl" && cfg.Security != "starttls" {
		return cfg, errors.New("security 应为 ssl 或 starttls")
	}
	return cfg, nil
}

type values []string

func (v *values) String() string { return strings.Join(*v, ", ") }
func (v *values) Set(s string) error {
	*v = append(*v, s)
	return nil
}

type options struct {
	to, images, attachments values
	subject, body, htmlFile string
	hasBody                 bool
}

func parseOptions() (options, error) {
	var o options
	flag.Var(&o.to, "to", "收件人邮箱，可重复传入")
	flag.StringVar(&o.subject, "subject", "", "邮件标题（必填）")
	flag.StringVar(&o.body, "body", "", "纯文本正文")
	flag.StringVar(&o.htmlFile, "html-file", "", "UTF-8 编码的 HTML 正文文件")
	flag.Var(&o.images, "image", "内嵌图片 ID=PATH，在 HTML 中用 cid:ID 引用，可重复传入")
	flag.Var(&o.attachments, "attach", "附件路径，可重复传入")
	flag.Parse()
	flag.Visit(func(f *flag.Flag) {
		if f.Name == "body" {
			o.hasBody = true
		}
	})
	if flag.NArg() > 0 || len(o.to) == 0 || strings.TrimSpace(o.subject) == "" {
		return o, errors.New("需要 --to 和 --subject，所有内容请通过命名参数传入")
	}
	if strings.IndexFunc(o.subject, unicode.IsControl) >= 0 {
		return o, errors.New("邮件标题包含控制字符")
	}
	if !utf8.ValidString(o.subject) || !utf8.ValidString(o.body) {
		return o, errors.New("标题和正文必须为 UTF-8 编码")
	}
	for _, recipient := range o.to {
		if !mailbox(recipient) {
			return o, fmt.Errorf("无效收件地址 %q，每个 --to 只接受一个邮箱地址", recipient)
		}
	}
	if len(o.images) > 0 && o.htmlFile == "" {
		return o, errors.New("--image 需要搭配 --html-file 使用")
	}
	return o, nil
}

type part struct {
	header textproto.MIMEHeader
	body   []byte
}

// Each 57-byte block produces one 76-character MIME base64 line.
func encodedPart(mediaType string, data []byte) part {
	var body bytes.Buffer
	for len(data) > 0 {
		n := 57
		if len(data) < n {
			n = len(data)
		}
		if body.Len() > 0 {
			body.WriteString("\r\n")
		}
		body.WriteString(base64.StdEncoding.EncodeToString(data[:n]))
		data = data[n:]
	}
	header := make(textproto.MIMEHeader)
	header.Set("Content-Type", mediaType)
	header.Set("Content-Transfer-Encoding", "base64")
	return part{header, body.Bytes()}
}

func textPart(subtype, content string) part {
	content = strings.ReplaceAll(content, "\r\n", "\n")
	content = strings.ReplaceAll(content, "\r", "\n")
	return encodedPart("text/"+subtype+"; charset=utf-8", []byte(strings.ReplaceAll(content, "\n", "\r\n")))
}

func filePart(path, cid string) (part, error) {
	name := filepath.Base(path)
	if strings.IndexFunc(name, unicode.IsControl) >= 0 {
		return part{}, errors.New("图片和附件的文件名不能包含控制字符")
	}
	mediaType := mime.TypeByExtension(strings.ToLower(filepath.Ext(path)))
	if mediaType == "" {
		mediaType = "application/octet-stream"
	}
	disposition := "attachment"
	if cid != "" {
		if !strings.HasPrefix(mediaType, "image/") {
			return part{}, fmt.Errorf("文件扩展名不是图片类型: %s", path)
		}
		disposition = "inline"
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return part{}, err
	}
	p := encodedPart(mediaType, data)
	p.header.Set("Content-Disposition", mime.FormatMediaType(disposition, map[string]string{"filename": name}))
	if cid != "" {
		p.header.Set("Content-ID", "<"+cid+">")
	}
	return p, nil
}

func multipartOf(subtype string, parts []part) part {
	var body bytes.Buffer
	mw := multipart.NewWriter(&body)
	for _, p := range parts {
		// These writes go to an in-memory bytes.Buffer.
		w, _ := mw.CreatePart(p.header)
		w.Write(p.body)
	}
	mw.Close()
	header := make(textproto.MIMEHeader)
	params := map[string]string{"boundary": mw.Boundary()}
	if subtype == "related" {
		params["type"] = "text/html"
	}
	header.Set("Content-Type", mime.FormatMediaType("multipart/"+subtype, params))
	return part{header, body.Bytes()}
}

var cidPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.@-]*$`)

func htmlPart(o options) (part, error) {
	html, err := os.ReadFile(o.htmlFile)
	if err != nil {
		return part{}, err
	}
	if !utf8.Valid(html) {
		return part{}, errors.New("HTML 正文文件必须为 UTF-8 编码")
	}
	p := textPart("html", string(html))
	if len(o.images) == 0 {
		return p, nil
	}
	parts := []part{p}
	ids := make(map[string]bool)
	for _, spec := range o.images {
		id, path, found := strings.Cut(spec, "=")
		if !found || path == "" || !cidPattern.MatchString(id) || ids[id] {
			return part{}, fmt.Errorf("无效或重复的内嵌图片参数 %q，请使用 ID=PATH", spec)
		}
		ids[id] = true
		img, err := filePart(path, id)
		if err != nil {
			return part{}, err
		}
		parts = append(parts, img)
	}
	return multipartOf("related", parts), nil
}

func messageBody(o options) (part, error) {
	body := textPart("plain", o.body)
	if o.htmlFile != "" {
		html, err := htmlPart(o)
		if err != nil {
			return part{}, err
		}
		if o.hasBody {
			body = multipartOf("alternative", []part{body, html})
		} else {
			body = html
		}
	}
	if len(o.attachments) == 0 {
		return body, nil
	}
	parts := []part{body}
	for _, path := range o.attachments {
		attachment, err := filePart(path, "")
		if err != nil {
			return part{}, err
		}
		parts = append(parts, attachment)
	}
	return multipartOf("mixed", parts), nil
}

// Fold long subjects into encoded words without splitting a UTF-8 character.
func subjectHeader(s string) string {
	if len(s) <= 68 {
		return mime.BEncoding.Encode("UTF-8", s)
	}
	var words []string
	for len(s) > 0 {
		n := len(s)
		if n > 42 {
			n = 42
			for !utf8.RuneStart(s[n]) {
				n--
			}
		}
		words = append(words, "=?UTF-8?B?"+base64.StdEncoding.EncodeToString([]byte(s[:n]))+"?=")
		s = s[n:]
	}
	return strings.Join(words, "\r\n ")
}

func buildMessage(sender string, o options) ([]byte, error) {
	body, err := messageBody(o)
	if err != nil {
		return nil, err
	}
	var id [16]byte
	if _, err = rand.Read(id[:]); err != nil {
		return nil, err
	}
	domain := sender[strings.LastIndex(sender, "@")+1:]
	var msg bytes.Buffer
	fmt.Fprintf(&msg, "From: %s\r\nTo: %s\r\n", sender, strings.Join(o.to, ",\r\n "))
	fmt.Fprintf(&msg, "Subject: %s\r\nDate: %s\r\n", subjectHeader(o.subject), time.Now().Format(time.RFC1123Z))
	fmt.Fprintf(&msg, "Message-ID: <%x@%s>\r\nMIME-Version: 1.0\r\n", id, domain)
	for name := range body.header {
		fmt.Fprintf(&msg, "%s: %s\r\n", name, body.header.Get(name))
	}
	msg.WriteString("\r\n")
	msg.Write(body.body)
	return msg.Bytes(), nil
}

// LOGIN is used by servers that do not offer the standard PLAIN mechanism.
type loginAuth struct{ username, password string }

func (a loginAuth) Start(info *smtp.ServerInfo) (string, []byte, error) {
	if !info.TLS {
		return "", nil, errors.New("LOGIN 需要 TLS")
	}
	return "LOGIN", nil, nil
}
func (a loginAuth) Next(challenge []byte, more bool) ([]byte, error) {
	if !more {
		return nil, nil
	}
	switch strings.ToLower(strings.TrimRight(strings.TrimSpace(string(challenge)), ":")) {
	case "username":
		return []byte(a.username), nil
	case "password":
		return []byte(a.password), nil
	default:
		return nil, errors.New("无法识别 LOGIN 认证提示")
	}
}

func authenticate(c *smtp.Client, cfg config) error {
	ok, methods := c.Extension("AUTH")
	if !ok {
		return errors.New("服务器未提供 SMTP AUTH")
	}
	var auth smtp.Auth
	for _, method := range strings.Fields(strings.ToUpper(methods)) {
		if method == "PLAIN" {
			auth = smtp.PlainAuth("", cfg.Username, cfg.Password, cfg.Host)
			break
		}
		if method == "LOGIN" {
			auth = loginAuth{cfg.Username, cfg.Password}
		}
	}
	if auth == nil {
		return errors.New("服务器未提供 PLAIN 或 LOGIN 认证")
	}
	if err := c.Auth(auth); err != nil {
		return errors.New("SMTP 认证失败，请检查邮箱地址、授权码和 SMTP 设置")
	}
	return nil
}

func send(cfg config, recipients []string, message []byte) error {
	tlsConfig := &tls.Config{ServerName: cfg.Host, MinVersion: tls.VersionTLS12}
	address := net.JoinHostPort(cfg.Host, strconv.Itoa(cfg.Port))
	dialer := &net.Dialer{Timeout: 30 * time.Second}
	var conn net.Conn
	var err error
	if cfg.Security == "ssl" {
		conn, err = tls.DialWithDialer(dialer, "tcp", address, tlsConfig)
	} else {
		conn, err = dialer.Dial("tcp", address)
	}
	if err != nil {
		return err
	}
	defer conn.Close()
	if err = conn.SetDeadline(time.Now().Add(2 * time.Minute)); err != nil {
		return err
	}
	c, err := smtp.NewClient(conn, cfg.Host)
	if err != nil {
		return err
	}
	if cfg.Security == "starttls" {
		if err = c.StartTLS(tlsConfig); err != nil {
			return fmt.Errorf("STARTTLS: %w", err)
		}
	}
	if err = authenticate(c, cfg); err != nil {
		return err
	}
	if err = c.Mail(cfg.Username); err != nil {
		return err
	}
	var refused []string
	for _, to := range recipients {
		if err = c.Rcpt(to); err != nil {
			var reply *textproto.Error
			if !errors.As(err, &reply) || reply.Code == 421 || reply.Code < 400 || reply.Code >= 600 {
				return fmt.Errorf("%s: %w", to, err)
			}
			refused = append(refused, to)
		}
	}
	if len(refused) == len(recipients) {
		return fmt.Errorf("服务器拒收以下地址：%s", strings.Join(refused, ", "))
	}
	w, err := c.Data()
	if err != nil {
		return err
	}
	if _, err = w.Write(message); err != nil {
		return err
	}
	if err = w.Close(); err != nil {
		return err
	}
	c.Quit() // The server has already accepted the message.
	if len(refused) > 0 {
		return fmt.Errorf("服务器已接受其余收件人，以下地址被拒收：%s", strings.Join(refused, ", "))
	}
	return nil
}

func run() error {
	opts, err := parseOptions()
	if err != nil {
		return err
	}
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	message, err := buildMessage(cfg.Username, opts)
	if err != nil {
		return err
	}
	return send(cfg, opts.to, message)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println("SMTP 服务器已接受邮件。")
}
