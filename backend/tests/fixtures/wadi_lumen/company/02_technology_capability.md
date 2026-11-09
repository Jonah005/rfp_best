# Technology Capability Statement

## Development stack

All portal work is developed in Microsoft C# on ASP.NET MVC 5 and ASP.NET Core,
with jQuery and plain JavaScript for client-side behaviour. Our developers hold
between four and eleven years of C# experience.

The standard database platform is Microsoft SQL Server 2016 or later. Every
portal we have delivered since 2016 runs on SQL Server, including audit and
activity logging tables written by the application.

Oracle: we have connected to an Oracle database once, in 2021, as a read-only
lookup from a SQL Server application through a linked server. We have not
built an application whose primary database is Oracle, and we have not written
to an Oracle database from our code.

## Hosting and deployment

We deploy to Windows Server 2016, 2019 and 2022 on IIS 10. We configure the
application pool to stay running (AlwaysRunning start mode, idle timeout
disabled), and enable static and dynamic compression in IIS.

We have not deployed to Windows Server 2012 since 2018, and our current build
pipeline targets .NET versions that Windows Server 2012 does not support.

SSL/TLS: we install the client's certificate in IIS, bind HTTPS, redirect all
HTTP traffic to HTTPS and disable TLS 1.0 and 1.1. The client purchases the
certificate.

## Front end

Sites are built responsive by default using Bootstrap 4 and 5. Menus collapse
to a single navigation button on screens below 768 pixels and content reflows
to the screen width. Right-to-left layout is supported for Arabic pages from a
shared template, so the Arabic and English sites use the same page structure.

We test on the current versions of Chrome, Edge, Safari and Firefox, and on
Safari for iOS and Chrome for Android. We do not test on Internet Explorer.

## Content management

Our portals include an administration area where authorised staff edit page
content with a rich text editor, manage navigation menus, and create users
with one of several roles (for example Administrator, Content Editor,
Reviewer). Role permissions are configured per page and per function.
