-- Calibration may sample songs from the libraries of users who opted in (users.calibration_consent), and only
-- those. Background work runs without a user context, so it normally sees no user rows at all; this policy lets
-- a transaction that declares app.purpose = 'calibration' read library rows of consenting users only.
CREATE POLICY calibration_read ON user_library FOR SELECT
    USING (current_setting('app.purpose', true) = 'calibration'
           AND EXISTS (SELECT 1 FROM users u WHERE u.id = user_library.user_id AND u.calibration_consent));
