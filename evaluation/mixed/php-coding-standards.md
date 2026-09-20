# PHP coding standards

## Scope
These rules apply to the PHP code of the customer portal and the partner integrations. They exist so that any developer can read any file of the portal without learning a private style first.

## Language version
The portal targets PHP 8.3. Use typed properties, return types and readonly classes wherever they fit. New code must pass the static analyser at the strictest level, and old code is brought up to that level file by file whenever it is touched.

## Frameworks
The customer portal is a Laravel application. The partner gateway is built on Symfony components, and small internal tools use Slim. A feature may not depend on framework internals: keep the business rules in plain classes so that they can move between frameworks. Controllers only translate the request into a call and the result into a response.

## Naming
Classes use StudlyCase, methods and variables use camelCase, and database columns use snake_case. A name says what a thing is, not how it is built: prefer InvoiceRepository to MysqlInvoiceTable. Avoid abbreviations except those everybody on the team already knows, such as id and url.

## Testing
Every bug fix starts with a failing test. Unit tests must not touch the network or the database. Integration tests run against a real database that CI starts in a Docker container, and each test cleans up after itself so the order of tests never matters.

## Error handling
Throw exceptions for conditions the caller cannot fix and return result objects for conditions the caller is expected to handle. Never catch an exception just to hide it. Log the exception once, at the place where it is finally handled, together with the request identifier.

## Code review
Every change is reviewed by one other developer before it is merged. A review looks at the behaviour, the tests and the names, in that order. Style problems that a tool can find are the tool's job, not the reviewer's.
